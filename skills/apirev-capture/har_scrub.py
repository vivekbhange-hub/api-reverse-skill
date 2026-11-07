#!/usr/bin/env python3
"""har_scrub: write a scrubbed copy of a HAR file that is safe to infer a spec from.

Redacts credentials by header, cookie, query and JSON key names (Authorization, Cookie,
Set-Cookie, X-API-Key, token, password, secret, ...) and by value pattern anywhere in a string
(JWTs, bearer tokens, AWS access keys, provider-style secret keys, long hex and base64 secrets).
With --pii it also replaces email addresses and phone numbers. Drops static assets (images,
fonts, CSS, JS, HTML pages) and, with --include-host, every host not on the list.

The original file is never modified. Standard library only, deterministic.
"""
from __future__ import annotations

import argparse
import base64
import copy
import fnmatch
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote_plus, unquote_plus, urlsplit, urlunsplit

MARK = "REDACTED"
EMAIL_PLACEHOLDER = "redacted@example.invalid"
PHONE_PLACEHOLDER = "REDACTED-phone"

SENSITIVE_HEADER = re.compile(
    r"(authorization|cookie|api[-_]?key|apikey|token|secret|session|signature|passw|csrf|xsrf|credential)",
    re.I)
SENSITIVE_QUERY = re.compile(
    r"(token|secret|signature|passw|api[-_]?key|apikey|session|credential|^key$|^sig$|^auth$|"
    r"^code$|^sid$|^pwd$|^x-amz-)", re.I)
SENSITIVE_KEY = re.compile(
    r"(passw|secret|token|api[-_]?key|apikey|access[-_]?key|private[-_]?key|authorization|"
    r"session[-_]?id|credential|signature|^otp$|^pin$|^cvv$|^ssn$|card[-_]?number)", re.I)
SAFE_KEY_SUFFIX = re.compile(r"(type|expires|expires_in|expiry|ttl|count|length|url|uri)$", re.I)

PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     MARK + "-private-key"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), MARK + "-jwt"),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MARK + "-aws-key"),
    ("bearer token", re.compile(r"(?i)\b(bearer)\s+(?!REDACTED)[A-Za-z0-9._~+/-]{8,}=*"), r"\1 " + MARK),
    ("provider secret key",
     re.compile(r"\b(?:(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                r"xox[abprs]-[A-Za-z0-9-]{10,})\b"),
     MARK + "-key"),
    ("hex secret", re.compile(r"\b[0-9a-fA-F]{32,}\b"), MARK + "-hex"),
    ("base64 secret",
     re.compile(r"(?<![A-Za-z0-9+/_-])(?=[A-Za-z0-9+/_-]*[A-Z])(?=[A-Za-z0-9+/_-]*[a-z])"
                r"(?=[A-Za-z0-9+/_-]*\d)[A-Za-z0-9+/_-]{40,}={0,2}"),
     MARK + "-secret"),
]
RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RE_PHONE = re.compile(r"\+?\(?\d[\d ().-]{8,}\d")
RE_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")

STATIC_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".avif", ".css", ".js",
    ".mjs", ".map", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".html", ".htm", ".mp4", ".webm",
    ".mp3", ".wasm",
}
STATIC_RESOURCE_TYPES = {"image", "font", "stylesheet", "script", "document", "media", "manifest"}
PRIVATE_ENTRY_FIELDS = ("serverIPAddress", "connection")


def media_type(value) -> str:
    return (value or "").split(";")[0].strip().lower()


def is_static(entry: dict) -> str:
    """Return a short label when the entry is a static asset or page, else ''."""
    req = entry.get("request") or {}
    resp = entry.get("response") or {}
    path = urlsplit(req.get("url") or "").path
    suffix = Path(path).suffix.lower()
    mime = media_type((resp.get("content") or {}).get("mimeType"))
    rtype = str(entry.get("_resourceType") or "").lower()
    if mime.startswith("image/") or rtype == "image" or suffix in {".png", ".jpg", ".jpeg", ".gif",
                                                                   ".svg", ".webp", ".ico", ".bmp",
                                                                   ".avif"}:
        return "image"
    if mime.startswith("font/") or rtype == "font" or suffix in {".woff", ".woff2", ".ttf", ".otf", ".eot"}:
        return "font"
    if mime == "text/css" or rtype == "stylesheet" or suffix == ".css":
        return "css"
    if "javascript" in mime or rtype == "script" or suffix in {".js", ".mjs", ".map", ".wasm"}:
        return "script"
    if mime in ("text/html", "application/xhtml+xml") or rtype == "document" or suffix in {".html", ".htm"}:
        return "html"
    if mime.startswith(("audio/", "video/")) or rtype in ("media", "manifest") or suffix in STATIC_EXTENSIONS:
        return "media"
    return ""


class Scrubber:
    def __init__(self, pii: bool = False):
        self.pii = pii
        self.redacted = Counter()
        self.pii_seen = Counter()

    # ---- strings
    def text(self, value: str) -> str:
        """Apply value patterns (and PII rules) to any free-form string."""
        if not isinstance(value, str) or not value:
            return value
        for label, pattern, replacement in PATTERNS:
            value, n = pattern.subn(replacement, value)
            if n:
                self.redacted[f"pattern: {label}"] += n
        return self._pii(value)

    def _pii(self, value: str) -> str:
        def email(match):
            if match.group(0) == EMAIL_PLACEHOLDER:
                return match.group(0)
            self.pii_seen["email"] += 1
            return EMAIL_PLACEHOLDER if self.pii else match.group(0)

        def phone(match):
            raw = match.group(0)
            digits = sum(c.isdigit() for c in raw)
            separators = sum(c in " ().-" for c in raw)
            if not 10 <= digits <= 15 or RE_DATE_PREFIX.match(raw):
                return raw
            if not raw.startswith("+") and separators < 2:
                return raw
            self.pii_seen["phone"] += 1
            return PHONE_PLACEHOLDER if self.pii else raw

        value = RE_EMAIL.sub(email, value)
        return RE_PHONE.sub(phone, value)

    # ---- structured data
    def json_value(self, value, key: str = ""):
        if isinstance(value, dict):
            return {k: self.json_value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.json_value(v, key) for v in value]
        if isinstance(value, str):
            if key and SENSITIVE_KEY.search(key) and not SAFE_KEY_SUFFIX.search(key) and MARK not in value:
                self.redacted[f"json key: {key.lower()}"] += 1
                return MARK
            return self.text(value)
        return value

    def header(self, name: str, value: str) -> str:
        low = name.lower()
        if not isinstance(value, str) or MARK in value:
            return value
        if low in ("authorization", "proxy-authorization"):
            self.redacted[f"header: {low}"] += 1
            parts = value.strip().split(" ", 1)
            if len(parts) == 2 and parts[0].isalpha():
                tag = "-jwt" if PATTERNS[1][1].search(parts[1]) else ""
                return f"{parts[0]} {MARK}{tag}"
            return MARK
        if low == "cookie":
            self.redacted[f"header: {low}"] += 1
            pieces = [p.strip() for p in value.split(";") if p.strip()]
            return "; ".join(f"{p.split('=', 1)[0]}={MARK}" if "=" in p else MARK for p in pieces)
        if low == "set-cookie":
            self.redacted[f"header: {low}"] += 1
            first, _, rest = value.partition(";")
            name_part = first.split("=", 1)[0].strip()
            return f"{name_part}={MARK}" + (";" + rest if rest else "")
        if SENSITIVE_HEADER.search(low):
            self.redacted[f"header: {low}"] += 1
            return MARK
        if low in ("referer", "location", "origin"):
            return self.url(value)
        return self.text(value)

    def query_value(self, key: str, value: str) -> str:
        if MARK in value:
            return value
        if SENSITIVE_QUERY.search(key):
            self.redacted[f"query: {key.lower()}"] += 1
            return MARK
        return self.text(value)

    def url(self, url: str) -> str:
        parts = urlsplit(url)
        if not parts.query:
            return url
        pieces = []
        for piece in parts.query.split("&"):
            raw_key, sep, raw_value = piece.partition("=")
            key, value = unquote_plus(raw_key), unquote_plus(raw_value)
            new = self.query_value(key, value) if sep else value
            pieces.append(piece if new == value else f"{raw_key}={quote_plus(new, safe='@')}")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(pieces), parts.fragment))

    def body_text(self, mime: str, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        if "json" in mime or text.strip()[:1] in ("{", "["):
            try:
                data = json.loads(text)
            except ValueError:
                return self.text(text)
            cleaned = self.json_value(data)
            if cleaned == data:
                return text
            return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
        if mime == "application/x-www-form-urlencoded":
            pieces = []
            for piece in text.split("&"):
                raw_key, sep, raw_value = piece.partition("=")
                key, value = unquote_plus(raw_key), unquote_plus(raw_value)
                new = self.query_value(key, value) if sep else value
                pieces.append(piece if new == value else f"{raw_key}={quote_plus(new, safe='@')}")
            return "&".join(pieces)
        return self.text(text)

    def entry(self, entry: dict) -> dict:
        for key in [k for k in entry if k.startswith("_") or k in PRIVATE_ENTRY_FIELDS]:
            del entry[key]
        req = entry.get("request") or {}
        resp = entry.get("response") or {}
        for part in (req, resp):
            for key in [k for k in part if k.startswith("_")]:
                del part[key]
            for header in part.get("headers") or []:
                header["value"] = self.header(str(header.get("name", "")), header.get("value", ""))
            for cookie in part.get("cookies") or []:
                if cookie.get("value") and MARK not in str(cookie.get("value")):
                    self.redacted[f"cookie: {str(cookie.get('name', '')).lower()}"] += 1
                    cookie["value"] = MARK
        if req.get("url"):
            req["url"] = self.url(req["url"])
        for item in req.get("queryString") or []:
            item["value"] = self.query_value(str(item.get("name", "")), str(item.get("value", "")))
        post = req.get("postData")
        if isinstance(post, dict):
            mime = media_type(post.get("mimeType"))
            if "text" in post:
                post["text"] = self.body_text(mime, post["text"])
            for param in post.get("params") or []:
                if "value" in param:
                    param["value"] = self.query_value(str(param.get("name", "")), str(param["value"]))
        if resp.get("redirectURL"):
            resp["redirectURL"] = self.url(resp["redirectURL"])
        content = resp.get("content")
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            mime = media_type(content.get("mimeType"))
            text = content["text"]
            if content.get("encoding") == "base64":
                try:
                    text = base64.b64decode(text).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    text = None
                if text is not None:
                    content.pop("encoding")
            if text is not None:
                content["text"] = self.body_text(mime, text)
        return entry


def host_of(entry: dict) -> str:
    return (urlsplit((entry.get("request") or {}).get("url") or "").hostname or "").lower()


def scrub_har(har: dict, include_hosts=(), pii: bool = False, keep_static: bool = False):
    """Return (scrubbed_copy, summary). The input object is not modified."""
    out = copy.deepcopy(har)
    log = out.get("log") or {}
    scrubber = Scrubber(pii=pii)
    hosts_seen, dropped_static, dropped_hosts = Counter(), Counter(), Counter()
    kept = []
    entries = log.get("entries") or []
    for entry in entries:
        host = host_of(entry)
        hosts_seen[host] += 1
        if include_hosts and not any(fnmatch.fnmatch(host, h.lower()) for h in include_hosts):
            dropped_hosts[host] += 1
            continue
        label = "" if keep_static else is_static(entry)
        if label:
            dropped_static[label] += 1
            continue
        kept.append(scrubber.entry(entry))
    log["entries"] = kept
    if "pages" in log:
        log["pages"] = [{k: v for k, v in page.items() if not k.startswith("_")}
                        for page in log.get("pages") or []]
    summary = {
        "entries_in": len(entries), "entries_kept": len(kept),
        "dropped_static": dict(sorted(dropped_static.items())),
        "dropped_hosts": dict(sorted(dropped_hosts.items())),
        "hosts_seen": dict(sorted(hosts_seen.items())),
        "redacted": dict(sorted(scrubber.redacted.items())),
        "pii_found": dict(sorted(scrubber.pii_seen.items())), "pii_redacted": pii,
    }
    return out, summary


def format_summary(summary: dict, source: str, target: str) -> str:
    lines = [f"Scrubbed {source} -> {target}",
             f"Kept {summary['entries_kept']} of {summary['entries_in']} entries."]
    if summary["dropped_static"]:
        lines.append("Dropped static: " + ", ".join(f"{n} {k}" for k, n in summary["dropped_static"].items()))
    if summary["dropped_hosts"]:
        lines.append("Dropped hosts not in --include-host: "
                     + ", ".join(f"{h} ({n})" for h, n in summary["dropped_hosts"].items()))
    elif len(summary["hosts_seen"]) > 1:
        lines.append("Hosts kept: " + ", ".join(f"{h} ({n})" for h, n in summary["hosts_seen"].items())
                     + ". Use --include-host to drop third-party hosts.")
    if summary["redacted"]:
        lines.append("Redacted:")
        lines += [f"  {label}: {n}" for label, n in summary["redacted"].items()]
    else:
        lines.append("Redacted: nothing matched the credential rules.")
    if summary["pii_found"]:
        found = ", ".join(f"{n} {k}" for k, n in summary["pii_found"].items())
        if summary["pii_redacted"]:
            lines.append(f"PII replaced (--pii): {found}.")
        else:
            lines.append(f"WARNING: PII found and NOT replaced: {found}. Rerun with --pii before sharing.")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="har_scrub.py",
        description="Write a scrubbed copy of a HAR file: credentials redacted, static assets and "
                    "third-party hosts dropped. The input file is never modified.")
    parser.add_argument("har", help="input HAR file")
    parser.add_argument("-o", "--output", default="api/capture.scrubbed.har",
                        help="output path (default: api/capture.scrubbed.har)")
    parser.add_argument("--include-host", action="append", default=[],
                        help="keep only these hosts; repeatable, wildcards like *.example.com allowed")
    parser.add_argument("--pii", action="store_true", help="also replace emails and phone numbers")
    parser.add_argument("--keep-static", action="store_true", help="keep images, fonts, CSS, JS, HTML")
    parser.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--quiet", action="store_true", help="print nothing on success")
    args = parser.parse_args(argv)

    source, target = Path(args.har), Path(args.output)
    if source.resolve() == target.resolve():
        print("error: output path is the input file; har_scrub.py never overwrites the original",
              file=sys.stderr)
        return 2
    try:
        har = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(har, dict) or not isinstance(har.get("log"), dict):
            raise ValueError("missing top-level 'log' object")
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {source}: {exc}", file=sys.stderr)
        return 2
    scrubbed, summary = scrub_har(har, args.include_host, args.pii, args.keep_static)
    if not args.dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(scrubbed, indent=2, ensure_ascii=False) + "\n"
        target.write_bytes(text.encode("utf-8"))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    elif not args.quiet:
        print(format_summary(summary, source.name, "(dry run)" if args.dry_run else target.as_posix()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

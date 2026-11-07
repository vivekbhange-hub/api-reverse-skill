#!/usr/bin/env python3
"""har2openapi: infer an OpenAPI 3.0.3 spec from a (scrubbed) HAR file.

Groups requests by host + method + path template, infers path parameters (numeric ids, UUIDs,
hex hashes, ISO dates, and slugs by cardinality), merges JSON schemas across every sample,
detects auth schemes and pagination, and writes:

  <out>/openapi.json          the spec, keys and paths sorted so diffs stay clean
  <out>/openapi.yaml          the same spec as YAML (with --yaml; stdlib emitter)
  <out>/inference-report.md   sample counts, how each template was inferred, low-confidence flags

Standard library only, no network, deterministic: the same HAR always gives the same bytes.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

OPENAPI_VERSION = "3.0.3"
DEFAULT_MIN_DISTINCT = 3
ENUM_MIN_SAMPLES = 5
ENUM_MAX_DISTINCT = 5
ENUM_MAX_LENGTH = 40
STRING_TRACK_LIMIT = 50
EXAMPLE_ARRAY_ITEMS = 2
REQUIRED_MIN_SAMPLES = 3
VALUES_SHOWN = 5

RE_INT = re.compile(r"^\d+$")
RE_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RE_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
RE_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")
RE_HEX = re.compile(r"^(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}$")
RE_VERSION = re.compile(r"^v\d+(\.\d+)*$", re.I)
RE_SLUG_SEP = re.compile(r"^[A-Za-z0-9]+(?:[-_.~][A-Za-z0-9]+)+$")
RE_ALNUM_MIX = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9]+$")
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RE_IPV4 = re.compile(r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$")
RE_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")
RE_QUERY_INT = re.compile(r"^-?(0|[1-9]\d*)$")
RE_QUERY_FLOAT = re.compile(r"^-?(0|[1-9]\d*)\.\d+$")
RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
RE_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
RE_APIKEY_HEADER = re.compile(r"(api[-_]?key|apikey|auth[-_]?token|access[-_]?token|session[-_]?token)")
RE_SESSION_COOKIE = re.compile(r"(sess|^sid$|token|auth|jwt)")

STATIC_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp", ".avif", ".css", ".js",
    ".mjs", ".map", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".html", ".htm", ".mp4", ".webm",
    ".mp3", ".wasm",
}
STATIC_RESOURCE_TYPES = {"image", "font", "stylesheet", "script", "document", "media", "manifest"}
CREDENTIAL_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key",
    "x-auth-token", "x-access-token",
}
API_KEY_QUERY = {"api_key", "apikey", "api-key", "key", "access_token", "token", "auth_token"}
PAGE_PARAMS = {"page", "page_number", "pagenumber", "pageno"}
SIZE_PARAMS = {"per_page", "perpage", "page_size", "pagesize", "limit", "size", "count", "max_results"}
OFFSET_PARAMS = {"offset", "skip", "start"}
CURSOR_PARAMS = {
    "cursor", "after", "before", "page_token", "pagetoken", "next_token", "starting_after",
    "ending_before", "continuation", "continuation_token",
}
NEXT_FIELDS = {
    "next", "next_cursor", "nextcursor", "next_page", "nextpage", "next_page_token",
    "nextpagetoken", "has_more", "hasmore", "next_url", "nexturl",
}
PAGINATION_CONTAINERS = {"meta", "pagination", "links", "paging", "page_info", "pageinfo"}
KIND_LABEL = {
    "integer": "integer id", "uuid": "UUID", "date": "ISO date", "hex": "hex hash",
    "slug": "slug", "string": "string",
}
KIND_SUFFIX = {"integer": "Id", "uuid": "Id", "hex": "Id", "date": "Date", "slug": "Slug", "string": "Id"}


# --------------------------------------------------------------------------- small helpers

def media_type(value) -> str:
    return (value or "").split(";")[0].strip().lower()


def is_json_mime(mime: str) -> bool:
    return mime == "application/json" or mime.endswith("+json") or "/json" in mime


def is_static_mime(mime: str) -> bool:
    return (mime.startswith(("image/", "font/", "audio/", "video/"))
            or "javascript" in mime
            or mime in {"text/css", "text/html", "application/xhtml+xml", "application/wasm"})


def value_sort_key(value: str):
    return (0, int(value), "") if RE_INT.match(value) else (1, 0, value)


def split_words(text: str) -> list:
    return [w for w in re.split(r"[^A-Za-z0-9]+", text) if w]


def singular(word: str) -> str:
    low = word.lower()
    if low.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if low.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if low.endswith("s") and not low.endswith(("ss", "us", "is")) and len(word) > 3:
        return word[:-1]
    return word


def is_plural(word: str) -> bool:
    return singular(word) != word


def pascal(text: str, singularize: bool = False) -> str:
    words = split_words(text)
    if singularize and words:
        words[-1] = singular(words[-1])
    return "".join(w[:1].upper() + w[1:] for w in words)


def camel(text: str, singularize: bool = False) -> str:
    name = pascal(text, singularize)
    if not name:
        return ""
    name = name[:1].lower() + name[1:]
    return "p" + name if name[0].isdigit() else name


def json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def string_format(value: str) -> str:
    if RE_UUID.match(value):
        return "uuid"
    if RE_DATETIME.match(value):
        return "date-time"
    if RE_DATE.match(value):
        return "date"
    if RE_EMAIL.match(value):
        return "email"
    if RE_IPV4.match(value):
        return "ipv4"
    if RE_URI.match(value):
        return "uri"
    return ""


def coerce_scalar(text: str):
    """Turn a query/form string into the JSON value it most likely carries."""
    if RE_QUERY_INT.match(text):
        return int(text)
    if RE_QUERY_FLOAT.match(text):
        return float(text)
    if text in ("true", "false"):
        return text == "true"
    return text


def trim_example(value):
    if isinstance(value, list):
        return [trim_example(v) for v in value[:EXAMPLE_ARRAY_ITEMS]]
    if isinstance(value, dict):
        return {k: trim_example(v) for k, v in value.items()}
    return value


def plural_s(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# --------------------------------------------------------------------------- path templates

def split_path(path: str) -> list:
    stripped = (path or "/").strip("/")
    return [unquote(s) for s in stripped.split("/")] if stripped else []


def classify_segment(segment: str):
    """Return the id kind of a path segment, or None when it looks like a literal."""
    if RE_INT.match(segment):
        return "integer"
    if RE_UUID.match(segment):
        return "uuid"
    if RE_DATE.match(segment):
        return "date"
    if RE_HEX.match(segment):
        return "hex"
    return None


def is_sluglike(segment: str) -> bool:
    if RE_VERSION.match(segment):
        return False
    return bool(RE_SLUG_SEP.match(segment) or RE_ALNUM_MIX.match(segment)
                or (len(segment) >= 16 and segment.isalnum()))


def _is_parent(token) -> bool:
    return (token[0] == "lit" and bool(token[1]) and not RE_VERSION.match(token[1])
            and token[1].lower() != "api")


def collapse_slugs(tokens: dict, min_distinct: int) -> None:
    """Turn literal segments into slug parameters by cardinality, in place.

    A position becomes a slug parameter when, among paths that agree on every other segment,
    it takes at least `min_distinct` different values, at least two thirds of them look like
    slugs (hyphenated words or mixed letters and digits), and the segment before it is a
    constant collection name (so `/v1/{x}` is never collapsed). Values that are followed by an
    id elsewhere (`/v1/pets/42`) are collections, not slugs, and are left alone.
    """
    if min_distinct <= 0 or not tokens:
        return
    width = max(len(t) for t in tokens.values())
    for i in range(1, width):
        collections = set()
        for t in tokens.values():
            if len(t) > i + 1 and t[i][0] == "lit" and t[i + 1][0] == "param":
                collections.add((tuple(t[:i]), t[i][1]))
        groups = {}
        for t in tokens.values():
            if len(t) <= i or t[i][0] != "lit" or not _is_parent(t[i - 1]):
                continue
            prefix = tuple(t[:i])
            if (prefix, t[i][1]) in collections:
                continue
            groups.setdefault((len(t), prefix, tuple(t[i + 1:])), set()).add(t[i][1])
        chosen = {}
        for (_, prefix, _), values in groups.items():
            slugs = sum(1 for v in values if is_sluglike(v))
            if len(values) >= min_distinct and slugs * 3 >= len(values) * 2:
                chosen.setdefault(prefix, set()).update(values)
        if not chosen:
            continue
        for t in tokens.values():
            if len(t) <= i or t[i][0] != "lit":
                continue
            prefix = tuple(t[:i])
            values = chosen.get(prefix)
            if (values is not None and (prefix, t[i][1]) not in collections
                    and (t[i][1] in values or is_sluglike(t[i][1]))):
                t[i] = ("param", "slug")


def parse_template_override(template: str) -> list:
    out = []
    for seg in split_path(template):
        if len(seg) > 2 and seg.startswith("{") and seg.endswith("}"):
            out.append(("named", seg[1:-1]))
        else:
            out.append(("lit", seg))
    return out


def _match_override(segments: list, overrides: list):
    for override in overrides:
        if len(override) == len(segments) and all(
                kind == "named" or value == seg for (kind, value), seg in zip(override, segments)):
            return override
    return None


class PathTemplate:
    """One inferred route: literal segments plus named parameters."""

    def __init__(self, segments: list, params: list):
        self.segments = segments  # [("lit", "pets"), ("param", "petId")]
        self.params = params      # [{"name", "kind", "position", "reason", "values"}]
        body = "/".join(v if k == "lit" else "{" + v + "}" for k, v in segments)
        self.path = "/" + body

    def __repr__(self) -> str:
        return f"PathTemplate({self.path!r})"


def _param_base(prev, kind: str) -> str:
    suffix = KIND_SUFFIX.get(kind, "Id")
    if prev is not None and _is_parent(prev):
        base = camel(prev[1], singularize=True)
        if base:
            return base + suffix
    return suffix[:1].lower() + suffix[1:]


def _unique(name: str, used: set) -> str:
    if name not in used:
        return name
    n = 2
    while f"{name}{n}" in used:
        n += 1
    return f"{name}{n}"


def infer_templates(raw_paths, min_distinct: int = DEFAULT_MIN_DISTINCT, overrides=()) -> dict:
    """Map every raw path to a PathTemplate. Paths of one host only."""
    parsed = [parse_template_override(o) for o in overrides]
    tokens, auto = {}, {}
    for raw in sorted(set(raw_paths)):
        segs = split_path(raw)
        override = _match_override(segs, parsed)
        if override is not None:
            tokens[raw] = list(override)
            continue
        toks = []
        for seg in segs:
            kind = classify_segment(seg)
            toks.append(("lit", seg) if kind is None else ("param", kind))
        tokens[raw] = auto[raw] = toks
    collapse_slugs(auto, min_distinct)

    groups = {}
    for raw in sorted(tokens):
        shape = tuple(tok if tok[0] in ("lit", "named") else ("param",) for tok in tokens[raw])
        groups.setdefault(shape, []).append(raw)

    result = {}
    for shape, raws in groups.items():
        segments, params, used = [], [], set()
        for i, tok in enumerate(shape):
            if tok[0] == "lit":
                segments.append(("lit", tok[1]))
                continue
            values = sorted({split_path(r)[i] for r in raws}, key=value_sort_key)
            if tok[0] == "named":
                name = tok[1]
                kinds = {classify_segment(v) for v in values}
                kind = next(iter(kinds)) if len(kinds) == 1 and None not in kinds else "string"
                reason = "--template override"
            else:
                kinds = sorted({tokens[r][i][1] for r in raws})
                kind = kinds[0] if len(kinds) == 1 else "string"
                name = _unique(_param_base(shape[i - 1] if i else None, kind), used)
                if len(kinds) > 1:
                    reason = "mixed (" + ", ".join(KIND_LABEL[k] for k in kinds) + ")"
                elif kind == "slug":
                    reason = f"slug by cardinality ({len(values)} distinct values, constant parent)"
                else:
                    reason = KIND_LABEL[kind]
            used.add(name)
            segments.append(("param", name))
            params.append({"name": name, "kind": kind, "position": i, "reason": reason,
                           "values": values})
        template = PathTemplate(segments, params)
        for raw in raws:
            result[raw] = template
    return result


def kind_schema(kind: str) -> dict:
    if kind == "integer":
        return {"type": "integer"}
    if kind == "uuid":
        return {"type": "string", "format": "uuid"}
    if kind == "date":
        return {"type": "string", "format": "date"}
    if kind == "hex":
        return {"type": "string", "pattern": "^[0-9a-fA-F]+$"}
    return {"type": "string"}


# --------------------------------------------------------------------------- schema merging

class Shape:
    """Accumulates every observed JSON value at one position, then becomes a schema."""

    def __init__(self):
        self.count = 0
        self.types = Counter()
        self.examples = {}
        self.strings = Counter()
        self.string_overflow = False
        self.formats = Counter()
        self.max_len = 0
        self.int_max = 0
        self.objects = 0
        self.props = {}
        self.presence = Counter()
        self.id_keys = 0
        self.name_keys = 0
        self.map_values = None
        self.arrays = 0
        self.items = None

    def observe(self, value) -> "Shape":
        self.count += 1
        kind = json_type(value)
        self.types[kind] += 1
        if kind in ("string", "integer", "number", "boolean") and kind not in self.examples:
            self.examples[kind] = value
        if kind == "string":
            if value in self.strings or len(self.strings) < STRING_TRACK_LIMIT:
                self.strings[value] += 1
            else:
                self.string_overflow = True
            self.formats[string_format(value)] += 1
            self.max_len = max(self.max_len, len(value))
        elif kind == "integer":
            self.int_max = max(self.int_max, abs(value))
        elif kind == "object":
            self.objects += 1
            for key, item in value.items():
                self.presence[key] += 1
                self.props.setdefault(key, Shape()).observe(item)
                if RE_INT.match(key) or RE_UUID.match(key):
                    self.id_keys += 1
                else:
                    self.name_keys += 1
                if self.map_values is None:
                    self.map_values = Shape()
                self.map_values.observe(item)
        elif kind == "array":
            self.arrays += 1
            for item in value:
                if self.items is None:
                    self.items = Shape()
                self.items.observe(item)
        return self


def to_schema(shape: Shape, pointer: str, flags: list, root_samples: int = 0) -> dict:
    """Convert a Shape into an OpenAPI 3.0 schema, appending (pointer, message) flags."""
    types = sorted(t for t in shape.types if t != "null")
    nullable = "null" in shape.types
    if not types:
        flags.append((pointer, "only null was observed, so the type is unknown"))
        return {"nullable": True}
    if "integer" in types and "number" in types:
        types.remove("integer")
    if len(types) == 1:
        schema = _typed_schema(shape, types[0], pointer, flags, root_samples)
    else:
        flags.append((pointer, "mixed types observed: " + ", ".join(types)))
        schema = {"oneOf": [_typed_schema(shape, t, pointer, flags, root_samples) for t in types]}
    if nullable:
        schema["nullable"] = True
    return schema


def _typed_schema(shape: Shape, kind: str, pointer: str, flags: list, root_samples: int) -> dict:
    if kind == "string":
        schema = {"type": "string"}
        seen = shape.types["string"]
        if len(shape.formats) == 1 and "" not in shape.formats:
            schema["format"] = next(iter(shape.formats))
        elif not shape.string_overflow and shape.max_len <= ENUM_MAX_LENGTH:
            distinct = len(shape.strings)
            if 2 <= distinct <= ENUM_MAX_DISTINCT and distinct * 2 <= seen and seen >= ENUM_MIN_SAMPLES:
                schema["enum"] = sorted(shape.strings)
            elif 2 <= distinct <= ENUM_MAX_DISTINCT and distinct < seen < ENUM_MIN_SAMPLES:
                values = ", ".join(sorted(shape.strings))
                flags.append((pointer, f"{distinct} distinct values in {seen} samples ({values}); "
                                       f"may be an enum but needs {ENUM_MIN_SAMPLES}+ samples, "
                                       "left as a free string"))
        if "string" in shape.examples:
            schema["example"] = shape.examples["string"]
        return schema
    if kind == "integer":
        schema = {"type": "integer"}
        if shape.int_max > 2147483647:
            schema["format"] = "int64"
        schema["example"] = shape.examples["integer"]
        return schema
    if kind == "number":
        schema = {"type": "number"}
        schema["example"] = shape.examples.get("number", shape.examples.get("integer"))
        return schema
    if kind == "boolean":
        return {"type": "boolean", "example": shape.examples["boolean"]}
    if kind == "array":
        if shape.items is None:
            flags.append((pointer, "only empty arrays observed, item type unknown"))
            return {"type": "array", "items": {}}
        return {"type": "array",
                "items": to_schema(shape.items, pointer + "[]", flags, root_samples or shape.arrays)}
    # object
    if shape.id_keys and not shape.name_keys and len(shape.props) >= 2:
        return {"type": "object",
                "additionalProperties": to_schema(shape.map_values, pointer + "{*}", flags,
                                                  root_samples)}
    schema = {"type": "object"}
    if shape.props:
        schema["properties"] = {k: to_schema(s, f"{pointer}.{k}", flags, root_samples or shape.objects)
                                for k, s in sorted(shape.props.items())}
    required = sorted(k for k, c in shape.presence.items() if c == shape.objects)
    if required:
        schema["required"] = required
        root = root_samples or shape.objects
        if shape.objects < REQUIRED_MIN_SAMPLES and (pointer == "$" or shape.objects < root):
            flags.append((pointer, f"required fields inferred from only "
                                   f"{plural_s(shape.objects, 'sample')}"))
    elif not shape.props:
        flags.append((pointer, "only empty objects observed"))
    return schema


# --------------------------------------------------------------------------- HAR reading

def load_har(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("log"), dict):
        raise ValueError("not a HAR file: missing top-level 'log' object")
    return data


def _headers(items) -> list:
    return [(str(h.get("name", "")), str(h.get("value", ""))) for h in items or []]


def normalize_entry(raw: dict) -> dict:
    req = raw.get("request") or {}
    resp = raw.get("response") or {}
    parts = urlsplit(req.get("url") or "")
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port else "")
    content = resp.get("content") or {}
    text = content.get("text")
    if text is not None and content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            text = None
    post = req.get("postData") or {}
    req_headers = _headers(req.get("headers"))
    resp_headers = _headers(resp.get("headers"))
    resp_mime = media_type(content.get("mimeType"))
    if not resp_mime:
        resp_mime = next((media_type(v) for n, v in resp_headers if n.lower() == "content-type"), "")
    cookies = [str(c.get("name", "")) for c in req.get("cookies") or []]
    for name, value in req_headers:
        if name.lower() == "cookie":
            cookies += [p.split("=", 1)[0].strip() for p in value.split(";") if "=" in p]
    return {
        "method": str(req.get("method") or "GET").upper(),
        "scheme": parts.scheme or "https",
        "host": host,
        "netloc": netloc,
        "path": parts.path or "/",
        "query": parse_qsl(parts.query, keep_blank_values=True),
        "headers": req_headers,
        "cookies": sorted(set(c for c in cookies if c)),
        "req_mime": media_type(post.get("mimeType")),
        "req_text": post.get("text"),
        "req_params": [(str(p.get("name", "")), str(p.get("value", "")), bool(p.get("fileName")))
                       for p in post.get("params") or []],
        "status": int(resp.get("status") or 0),
        "resp_mime": resp_mime,
        "resp_text": text,
        "resp_headers": resp_headers,
        "resource_type": str(raw.get("_resourceType") or "").lower(),
    }


def skip_reason(entry: dict):
    if entry["status"] == 0:
        return "no response recorded"
    if entry["method"] == "OPTIONS":
        return "CORS preflight"
    ext = Path(entry["path"]).suffix.lower()
    if ext in STATIC_EXTENSIONS or entry["resource_type"] in STATIC_RESOURCE_TYPES:
        return "static asset or page"
    if is_static_mime(entry["resp_mime"]):
        return "static asset or page"
    return None


def select_entries(har: dict, hosts=()) -> tuple:
    entries, skipped = [], Counter()
    for raw in har["log"].get("entries") or []:
        entry = normalize_entry(raw)
        if hosts and not any(fnmatch.fnmatch(entry["host"], h.lower()) for h in hosts):
            skipped["host not selected with --host"] += 1
            continue
        reason = skip_reason(entry)
        if reason:
            skipped[reason] += 1
            continue
        entries.append(entry)
    return entries, skipped


def find_unscrubbed(har: dict) -> list:
    """Reasons this HAR still looks like it carries live credentials."""
    reasons = set()
    for raw in har["log"].get("entries") or []:
        req = raw.get("request") or {}
        resp = raw.get("response") or {}
        blob = [str(req.get("url") or "")]
        for name, value in _headers(req.get("headers")) + _headers(resp.get("headers")):
            if name.lower() in CREDENTIAL_HEADERS and value.strip() and "REDACTED" not in value:
                reasons.add(f"live value in the `{name.lower()}` header")
            blob.append(value)
        blob.append(str((req.get("postData") or {}).get("text") or ""))
        blob.append(str((resp.get("content") or {}).get("text") or ""))
        text = "\n".join(blob)
        if RE_JWT.search(text):
            reasons.add("JWT-like token")
        if RE_AWS_KEY.search(text):
            reasons.add("AWS access key id")
    return sorted(reasons)


def parse_body(mime: str, text, params=()):
    """Return (kind, value): kind is json, form, text or None."""
    if mime.startswith("multipart/") and params:
        return "form", {name: ("" if is_file else coerce_scalar(value))
                        for name, value, is_file in params}
    if text is None or text == "":
        if params:
            return "form", {name: coerce_scalar(value) for name, value, _ in params}
        return None, None
    stripped = text.strip()
    if is_json_mime(mime) or (not mime and stripped[:1] in ("{", "[")):
        try:
            return "json", json.loads(text)
        except ValueError:
            return "text", text
    if mime == "application/x-www-form-urlencoded":
        out = {}
        for key, value in parse_qsl(text, keep_blank_values=True):
            coerced = coerce_scalar(value)
            if key in out:
                out[key] = (out[key] if isinstance(out[key], list) else [out[key]]) + [coerced]
            else:
                out[key] = coerced
        return "form", out
    return "text", text


# --------------------------------------------------------------------------- detection

def detect_security(entry: dict) -> dict:
    found = {}
    for name, value in entry["headers"]:
        low = name.lower()
        if low in ("authorization", "proxy-authorization"):
            word = value.strip().split(" ", 1)[0].lower() if value.strip() else ""
            if word == "bearer":
                scheme = {"type": "http", "scheme": "bearer"}
                if "jwt" in value.lower() or RE_JWT.search(value):
                    scheme["bearerFormat"] = "JWT"
                found["bearerAuth"] = scheme
            elif word == "basic":
                found["basicAuth"] = {"type": "http", "scheme": "basic"}
            elif low == "authorization":
                found["authorizationHeader"] = {"type": "apiKey", "in": "header", "name": name}
        elif RE_APIKEY_HEADER.search(low):
            found[camel(low) + "Header"] = {"type": "apiKey", "in": "header", "name": name}
    for key, _ in entry["query"]:
        if key.lower() in API_KEY_QUERY:
            found[camel(key) + "Query"] = {"type": "apiKey", "in": "query", "name": key}
    for cookie in entry["cookies"]:
        if RE_SESSION_COOKIE.search(cookie.lower()):
            found[camel(cookie) + "Cookie"] = {"type": "apiKey", "in": "cookie", "name": cookie}
    return found


def _next_fields(body) -> list:
    out = []
    if isinstance(body, dict):
        for key, value in body.items():
            if key.lower() in NEXT_FIELDS:
                out.append(key)
            elif key.lower() in PAGINATION_CONTAINERS and isinstance(value, dict):
                out += [f"{key}.{k}" for k in value if k.lower() in NEXT_FIELDS]
    return out


def detect_pagination(query_names, response_fields, link_header: bool):
    lower = {n.lower(): n for n in query_names}
    size = sorted(lower[n] for n in lower if n in SIZE_PARAMS)
    style, params = None, []
    if any(n in PAGE_PARAMS for n in lower):
        style, params = "page", sorted(lower[n] for n in lower if n in PAGE_PARAMS) + size
    elif any(n in OFFSET_PARAMS for n in lower):
        style, params = "offset", sorted(lower[n] for n in lower if n in OFFSET_PARAMS) + size
    elif any(n in CURSOR_PARAMS for n in lower):
        style, params = "cursor", sorted(lower[n] for n in lower if n in CURSOR_PARAMS) + size
    elif link_header:
        style = "link"
    elif response_fields:
        style = "cursor"
    if style is None:
        return None
    return {"style": style, "query": sorted(set(params)), "response": sorted(set(response_fields)),
            "linkHeader": bool(link_header)}


def describe_pagination(p: dict) -> str:
    label = {"page": "page-based", "offset": "offset-based", "cursor": "cursor-based",
             "link": "Link header"}[p["style"]]
    bits = []
    if p["query"]:
        bits.append("query " + ", ".join(f"`{q}`" for q in p["query"]))
    if p["response"]:
        bits.append("response field " + ", ".join(f"`{r}`" for r in p["response"]))
    if p["linkHeader"]:
        bits.append("`Link` header with rel=\"next\"")
    return f"{label} ({'; '.join(bits)})" if bits else label


# --------------------------------------------------------------------------- operations

class Operation:
    def __init__(self, method: str, template: PathTemplate):
        self.method = method
        self.template = template
        self.count = 0
        self.query = {}
        self.query_present = Counter()
        self.query_multi = set()
        self.query_values = {}
        self.auth_query = set()
        self.body_count = 0
        self.bodies = {}
        self.body_examples = {}
        self.responses = {}
        self.security = {}
        self.no_auth = 0
        self.next_fields = set()
        self.link_next = False

    def add(self, entry: dict, schemes: dict) -> None:
        self.count += 1
        if schemes:
            for key in schemes:
                self.security[key] = self.security.get(key, 0) + 1
        else:
            self.no_auth += 1
        auth_query = {s["name"] for s in schemes.values() if s.get("in") == "query"}
        self.auth_query |= auth_query
        grouped = {}
        for key, value in entry["query"]:
            if key not in auth_query:
                grouped.setdefault(key, []).append(value)
        for key, values in grouped.items():
            self.query_present[key] += 1
            if len(values) > 1 or key.endswith("[]"):
                self.query_multi.add(key)
            shape = self.query.setdefault(key, Shape())
            seen = self.query_values.setdefault(key, set())
            for value in values:
                shape.observe(coerce_scalar(value))
                seen.add(value)
        kind, body = parse_body(entry["req_mime"], entry["req_text"], entry["req_params"])
        if kind is not None:
            self.body_count += 1
            mime = entry["req_mime"] or ("application/json" if kind == "json" else "text/plain")
            self._observe(self.bodies, self.body_examples, mime, kind, body)
        status = entry["status"]
        slot = self.responses.setdefault(status, {"count": 0, "content": {}, "examples": {},
                                                  "link": False})
        slot["count"] += 1
        for name, value in entry["resp_headers"]:
            if name.lower() == "link" and re.search(r'rel="?next"?', value):
                slot["link"] = True
                if 200 <= status < 300:
                    self.link_next = True
        kind, body = parse_body(entry["resp_mime"], entry["resp_text"])
        if kind is not None and status != 204:
            mime = entry["resp_mime"] or ("application/json" if kind == "json" else "text/plain")
            self._observe(slot["content"], slot["examples"], mime, kind, body)
            if kind == "json" and 200 <= status < 300:
                self.next_fields.update(_next_fields(body))

    @staticmethod
    def _observe(store: dict, examples: dict, mime: str, kind: str, body) -> None:
        if kind in ("json", "form"):
            store.setdefault(mime, Shape()).observe(body)
            examples.setdefault(mime, body)
        else:
            store.setdefault(mime, None)

    def pagination(self):
        if self.method != "GET":
            return None
        return detect_pagination(self.query, sorted(self.next_fields), self.link_next)


def confidence(samples: int) -> str:
    return "high" if samples >= 5 else "medium" if samples >= 2 else "low"


def _op_id(template: PathTemplate, method: str, full: bool) -> str:
    lits = [v for k, v in template.segments
            if k == "lit" and v and not RE_VERSION.match(v) and v.lower() != "api"]
    if not lits:
        lits = ["root"]
    ends_param = bool(template.segments) and template.segments[-1][0] == "param"
    m = method.lower()
    verb = {"get": "get", "put": "replace", "patch": "update", "delete": "delete",
            "post": "post"}.get(m, m)
    parents = "".join(pascal(w, singularize=True) for w in lits[:-1]) if full else ""
    last = lits[-1]
    if ends_param:
        return verb + parents + pascal(last, singularize=True)
    if is_plural(last) and m == "get":
        return "list" + parents + pascal(last)
    if is_plural(last) and m == "post":
        return "create" + parents + pascal(last, singularize=True)
    if is_plural(last):
        return verb + parents + pascal(last)
    return verb + "".join(pascal(w) for w in lits)


def operation_ids(keys: list) -> dict:
    short = {k: _op_id(k[0], k[1], False) for k in keys}
    counts = Counter(short.values())
    ids = {k: (_op_id(k[0], k[1], True) if counts[short[k]] > 1 else short[k]) for k in keys}
    seen, final = Counter(), {}
    for k in keys:
        seen[ids[k]] += 1
        final[k] = ids[k] if seen[ids[k]] == 1 else f"{ids[k]}{seen[ids[k]]}"
    return final


# Fixed RFC 9110 reason phrases. http.HTTPStatus changes between Python versions (3.13 renamed
# 422), which would make the same HAR produce different specs on different interpreters.
STATUS_PHRASES = {
    100: "Continue", 101: "Switching Protocols", 200: "OK", 201: "Created", 202: "Accepted",
    203: "Non-Authoritative Information", 204: "No Content", 205: "Reset Content",
    206: "Partial Content", 207: "Multi-Status", 300: "Multiple Choices", 301: "Moved Permanently",
    302: "Found", 303: "See Other", 304: "Not Modified", 307: "Temporary Redirect",
    308: "Permanent Redirect", 400: "Bad Request", 401: "Unauthorized", 402: "Payment Required",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 406: "Not Acceptable",
    408: "Request Timeout", 409: "Conflict", 410: "Gone", 411: "Length Required",
    412: "Precondition Failed", 413: "Content Too Large", 414: "URI Too Long",
    415: "Unsupported Media Type", 416: "Range Not Satisfiable", 417: "Expectation Failed",
    418: "I'm a teapot", 421: "Misdirected Request", 422: "Unprocessable Content", 423: "Locked",
    424: "Failed Dependency", 425: "Too Early", 426: "Upgrade Required",
    428: "Precondition Required", 429: "Too Many Requests",
    431: "Request Header Fields Too Large", 451: "Unavailable For Legal Reasons",
    500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway",
    503: "Service Unavailable", 504: "Gateway Timeout", 505: "HTTP Version Not Supported",
}


def _status_phrase(status: int) -> str:
    return STATUS_PHRASES.get(status, f"Status {status}")


def _media(shape, example, pointer: str, flags: list) -> dict:
    if shape is None:
        return {"schema": {"type": "string"}}
    media = {"schema": to_schema(shape, "$", flags_for(flags, pointer))}
    if example is not None:
        media["example"] = trim_example(example)
    return media


def flags_for(flags: list, where: str):
    """Return a list-like whose append records (where, pointer, message)."""
    class _Sink(list):
        def append(self, item):
            flags.append((where, item[0], item[1]))
    return _Sink()


def build_spec(har: dict, title=None, api_version: str = "0.1.0", hosts=(),
               min_distinct: int = DEFAULT_MIN_DISTINCT, overrides=(), source_name: str = "capture.har"):
    entries, skipped = select_entries(har, hosts)
    by_host = {}
    for entry in entries:
        by_host.setdefault(entry["netloc"], []).append(entry)
    templates = {}
    for netloc in sorted(by_host):
        mapping = infer_templates([e["path"] for e in by_host[netloc]], min_distinct, overrides)
        for raw, tpl in mapping.items():
            templates[(netloc, raw)] = tpl

    ops, schemes, host_counts, host_scheme = {}, {}, Counter(), {}
    for entry in entries:
        tpl = templates[(entry["netloc"], entry["path"])]
        key = (tpl.path, entry["method"])
        if key not in ops:
            ops[key] = Operation(entry["method"], tpl)
        found = detect_security(entry)
        for name, scheme in found.items():
            if name in schemes and schemes[name].get("bearerFormat"):
                continue
            schemes[name] = scheme
        ops[key].add(entry, found)
        host_counts[entry["netloc"]] += 1
        host_scheme.setdefault(entry["netloc"], entry["scheme"])

    keys = sorted(ops)
    ids = operation_ids([(ops[k].template, k[1]) for k in keys])
    flags, paths, tags, endpoint_rows, template_rows, pagination_rows = [], {}, set(), [], {}, []
    security_use = Counter()
    for key in keys:
        op = ops[key]
        tpl = op.template
        where = f"{op.method} {tpl.path}"
        op_id = ids[(tpl, op.method)]
        lits = [v for k, v in tpl.segments
                if k == "lit" and v and not RE_VERSION.match(v) and v.lower() != "api"]
        tag = lits[0] if lits else "root"
        tags.add(tag)
        params = []
        for p in tpl.params:
            schema = kind_schema(p["kind"])
            schema["example"] = int(p["values"][0]) if p["kind"] == "integer" else p["values"][0]
            params.append({"name": p["name"], "in": "path", "required": True, "schema": schema,
                           "description": f"Inferred as {p['reason']}."})
            template_rows[(tpl.path, p["name"])] = p
        for name in sorted(op.query):
            shape = op.query[name]
            schema = to_schema(shape, "$", flags_for(flags, f"{where} query {name}"))
            if name in op.query_multi:
                schema = {"type": "array", "items": schema}
            values = sorted(op.query_values[name], key=value_sort_key)
            shown = ", ".join(values[:VALUES_SHOWN]) + (", ..." if len(values) > VALUES_SHOWN else "")
            params.append({
                "name": name, "in": "query", "required": op.query_present[name] == op.count,
                "schema": schema,
                "description": f"Seen in {op.query_present[name]} of {plural_s(op.count, 'request')}. "
                               f"Observed values: {shown}.",
            })
        operation = {
            "operationId": op_id,
            "tags": [tag],
            "responses": {},
            "x-apirev-samples": op.count,
            "x-apirev-confidence": confidence(op.count),
        }
        description = [f"Inferred from {plural_s(op.count, 'recorded request')}."]
        if params:
            operation["parameters"] = params
        if op.bodies:
            content = {mime: _media(shape, op.body_examples.get(mime), f"{where} request body", flags)
                       for mime, shape in sorted(op.bodies.items())}
            operation["requestBody"] = {"required": op.body_count == op.count, "content": content}
        for status in sorted(op.responses):
            slot = op.responses[status]
            response = {"description": _status_phrase(status)}
            if slot["content"]:
                response["content"] = {
                    mime: _media(shape, slot["examples"].get(mime), f"{where} response {status}", flags)
                    for mime, shape in sorted(slot["content"].items())}
            if slot["link"]:
                response["headers"] = {"Link": {"description": "Pagination links (RFC 8288).",
                                                "schema": {"type": "string"}}}
            operation["responses"][str(status)] = response
        if op.security:
            alternatives = [{name: []} for name in sorted(op.security)]
            if op.no_auth:
                alternatives.append({})
            operation["security"] = alternatives
            for name, n in op.security.items():
                security_use[name] += n
        pagination = op.pagination()
        if pagination:
            operation["x-apirev-pagination"] = pagination
            description.append("Paginated: " + describe_pagination(pagination) + ".")
            pagination_rows.append((where, pagination))
        operation["description"] = " ".join(description)
        paths.setdefault(tpl.path, {})[op.method.lower()] = operation
        endpoint_rows.append({
            "method": op.method, "path": tpl.path, "samples": op.count,
            "statuses": {s: op.responses[s]["count"] for s in sorted(op.responses)},
            "confidence": confidence(op.count), "operationId": op_id,
            "pagination": pagination,
        })

    hosts_sorted = sorted(host_counts, key=lambda h: (-host_counts[h], h))
    if not title:
        first = hosts_sorted[0] if hosts_sorted else "api"
        words = [w for w in first.split(":")[0].split(".") if w not in ("api", "www")]
        title = (words[0].capitalize() if words else "Inferred") + " API"
    spec = {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": title,
            "version": api_version,
            "description": (f"Inferred by har2openapi.py from {plural_s(len(entries), 'recorded request')} "
                            "in a HAR capture. Field names, types and required flags come from the "
                            "samples only; read inference-report.md before relying on it."),
        },
        "servers": [{"url": f"{host_scheme[h]}://{h}"} for h in hosts_sorted],
        "tags": [{"name": t} for t in sorted(tags)],
        "paths": paths,
    }
    if schemes:
        components = {}
        for name, scheme in sorted(schemes.items()):
            scheme = dict(scheme)
            scheme["description"] = (f"Detected on {plural_s(security_use[name], 'request')}. "
                                     "Credential values are never recorded in this spec.")
            components[name] = scheme
        spec["components"] = {"securitySchemes": components}
    report = {
        "title": title, "source": source_name, "entries": len(entries), "skipped": skipped,
        "hosts": [(h, host_counts[h]) for h in hosts_sorted], "endpoints": endpoint_rows,
        "templates": [(path, p) for (path, _), p in sorted(template_rows.items(),
                                                           key=lambda kv: (kv[0][0], kv[1]["position"]))],
        "security": [(name, spec["components"]["securitySchemes"][name], security_use[name])
                     for name in sorted(schemes)],
        "pagination": pagination_rows, "flags": sorted(set(flags)), "warnings": [],
        "min_distinct": min_distinct, "paths": len(paths), "operations": len(keys),
    }
    return spec, report


# --------------------------------------------------------------------------- output

def dump_json(spec: dict) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


YAML_PLAIN = re.compile(r"^[A-Za-z_/][A-Za-z0-9_./-]*$")
YAML_RESERVED = {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}
YAML_ESCAPES = {"\x7f": "\\x7f", "\u0085": "\\N", "\u2028": "\\L", "\u2029": "\\P"}


def yaml_scalar(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    text = str(value)
    if YAML_PLAIN.match(text) and text.lower() not in YAML_RESERVED:
        return text
    quoted = json.dumps(text, ensure_ascii=False)
    for raw, esc in YAML_ESCAPES.items():
        quoted = quoted.replace(raw, esc)
    return re.sub(r"[\x80-\x9f]", lambda m: "\\x%02x" % ord(m.group(0)), quoted)


def _yaml_lines(node, indent: int, lines: list) -> None:
    pad = " " * indent
    if isinstance(node, dict):
        for key, value in node.items():
            k = yaml_scalar(str(key))
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{k}:")
                _yaml_lines(value, indent + 2, lines)
            else:
                lines.append(f"{pad}{k}: {yaml_scalar(value)}")
    else:
        for item in node:
            if isinstance(item, (dict, list)) and item:
                sub = []
                _yaml_lines(item, indent + 2, sub)
                sub[0] = pad + "- " + sub[0][indent + 2:]
                lines.extend(sub)
            else:
                lines.append(f"{pad}- {yaml_scalar(item)}")


def to_yaml(data) -> str:
    """Emit JSON-compatible data as block YAML. Keys keep their (sorted) order."""
    canonical = json.loads(json.dumps(data, sort_keys=True))
    if not isinstance(canonical, (dict, list)) or not canonical:
        return yaml_scalar(canonical) + "\n"
    lines = []
    _yaml_lines(canonical, 0, lines)
    return "\n".join(lines) + "\n"


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_report(report: dict) -> str:
    out = [f"# Inference report: {report['title']}", ""]
    out.append(f"Generated by `har2openapi.py` from `{report['source']}`. Every number below comes "
               "from the recorded samples. The spec describes what was captured, not everything the "
               "API can do.")
    out.append("")
    for warning in report["warnings"]:
        out.append(f"> **Warning:** {warning}")
        out.append("")
    skipped = report["skipped"]
    skipped_text = ", ".join(f"{n} {reason}" for reason, n in sorted(skipped.items())) or "none"
    schemes = ", ".join(f"`{name}`" for name, _, _ in report["security"]) or "none detected"
    out += [
        "| Item | Value |", "|---|---|",
        f"| API requests used | {report['entries']} |",
        f"| Entries skipped | {skipped_text} |",
        "| Hosts | " + ", ".join(f"{h} ({n})" for h, n in report["hosts"]) + " |",
        f"| Paths / operations | {report['paths']} / {report['operations']} |",
        f"| Security schemes | {schemes} |",
        "",
        "## Endpoints",
        "",
        "Confidence: high = 5+ samples, medium = 2-4, low = 1.",
        "",
        "| Method | Path | operationId | Samples | Status codes | Confidence |",
        "|---|---|---|---:|---|---|",
    ]
    for row in report["endpoints"]:
        statuses = ", ".join(f"{s} ({n})" if n > 1 else str(s) for s, n in row["statuses"].items())
        out.append(f"| {row['method']} | `{row['path']}` | `{row['operationId']}` | {row['samples']} "
                   f"| {statuses} | {row['confidence']} |")
    out += ["", "## Path parameters", ""]
    if report["templates"]:
        out += ["| Template | Parameter | Detected as | Distinct values | Values seen |",
                "|---|---|---|---:|---|"]
        for path, p in report["templates"]:
            values = p["values"]
            shown = ", ".join(f"`{v}`" for v in values[:VALUES_SHOWN])
            if len(values) > VALUES_SHOWN:
                shown += ", ..."
            out.append(f"| `{path}` | `{p['name']}` | {_md(p['reason'])} | {len(values)} | {shown} |")
        out.append("")
        out.append(f"Slugs need {report['min_distinct']}+ distinct values under a constant parent "
                   "segment. Plain-word identifiers (usernames, codes) are not detected: pass "
                   "`--template '/users/{username}'` for those.")
    else:
        out.append("No path parameters were inferred.")
    out += ["", "## Security", ""]
    if report["security"]:
        for name, scheme, uses in report["security"]:
            if scheme["type"] == "http":
                kind = f"HTTP {scheme['scheme']}" + (f" ({scheme['bearerFormat']})"
                                                     if scheme.get("bearerFormat") else "")
            else:
                kind = f"API key in {scheme['in']} `{scheme['name']}`"
            out.append(f"- `{name}`: {kind}, seen on {plural_s(uses, 'request')}. No values recorded.")
    else:
        out.append("No authentication was detected in the captured requests.")
    out += ["", "## Pagination", ""]
    if report["pagination"]:
        for where, p in report["pagination"]:
            out.append(f"- `{where}`: {describe_pagination(p)}.")
    else:
        out.append("No pagination parameters, next-page fields or Link headers were seen.")
    out += ["", "## Low-confidence findings", ""]
    if report["flags"]:
        out.append("Check these before trusting the spec, or hand this report to the "
                   "`schema-skeptic` agent.")
        out.append("")
        for where, pointer, message in report["flags"]:
            at = "" if pointer == "$" else f" `{pointer}`"
            out.append(f"- `{where}`{at}: {message}.")
    else:
        out.append("None.")
    out += [
        "", "## Rules used", "",
        "- Path ids: all-digit segments, UUIDs, ISO dates and hex hashes (8+ chars, letters and "
        "digits) become parameters named after the segment before them (`/pets/{petId}`).",
        "- Required: a field or query parameter is required when it appeared in every sample.",
        "- Types: integers and decimals at one position merge to `number`; `null` makes a field "
        "`nullable`; different types become `oneOf`.",
        f"- Enums: only when a string field has {ENUM_MIN_SAMPLES}+ samples, at most "
        f"{ENUM_MAX_DISTINCT} distinct values, and every value repeats on average.",
        "- Formats: set only when every observed value matches (date-time, date, email, uuid, uri, "
        "ipv4).",
        "- Examples come from the (scrubbed) capture; arrays are trimmed to "
        f"{EXAMPLE_ARRAY_ITEMS} items.",
    ]
    return "\n".join(out) + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="har2openapi.py",
        description="Infer an OpenAPI 3.0.3 spec and an inference report from a scrubbed HAR file.")
    parser.add_argument("har", help="HAR file (run har_scrub.py on it first)")
    parser.add_argument("--out", default="api", help="output folder (default: api)")
    parser.add_argument("--title", help="info.title (default: derived from the main host)")
    parser.add_argument("--api-version", default="0.1.0", help="info.version (default: 0.1.0)")
    parser.add_argument("--host", action="append", default=[],
                        help="only use this host; repeatable, wildcards like *.example.com allowed")
    parser.add_argument("--template", action="append", default=[],
                        help="force a path template, e.g. '/users/{username}'; repeatable")
    parser.add_argument("--min-distinct", type=int, default=DEFAULT_MIN_DISTINCT,
                        help="distinct values needed before a segment is treated as a slug "
                             "(default: 3, 0 disables slug detection)")
    parser.add_argument("--yaml", action="store_true", help="also write openapi.yaml")
    parser.add_argument("--no-report", action="store_true", help="skip inference-report.md")
    parser.add_argument("--stdout", action="store_true", help="print the JSON spec, write nothing")
    parser.add_argument("--allow-unscrubbed", action="store_true",
                        help="process a HAR that still looks like it holds credentials")
    parser.add_argument("--quiet", action="store_true", help="no summary on stdout")
    args = parser.parse_args(argv)

    har_path = Path(args.har)
    try:
        har = load_har(har_path)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {har_path}: {exc}", file=sys.stderr)
        return 2
    problems = find_unscrubbed(har)
    if problems and not args.allow_unscrubbed:
        print("error: this HAR looks unscrubbed (" + "; ".join(problems) + ").\n"
              "Run har_scrub.py first so no credentials end up in the spec, or pass "
              "--allow-unscrubbed if you are sure.", file=sys.stderr)
        return 2
    spec, report = build_spec(har, args.title, args.api_version, args.host, args.min_distinct,
                              args.template, har_path.name)
    if problems:
        report["warnings"].append("Processed with --allow-unscrubbed and the HAR looked unscrubbed ("
                                  + "; ".join(problems) + "). Examples may contain credentials.")
    if not spec["paths"]:
        print("error: no API requests found (every entry was static, filtered or empty)",
              file=sys.stderr)
        return 1
    if args.stdout:
        sys.stdout.write(dump_json(spec))
        return 0
    out = Path(args.out)
    written = [out / "openapi.json"]
    write_text(out / "openapi.json", dump_json(spec))
    if args.yaml:
        write_text(out / "openapi.yaml", to_yaml(spec))
        written.append(out / "openapi.yaml")
    if not args.no_report:
        write_text(out / "inference-report.md", render_report(report))
        written.append(out / "inference-report.md")
    if not args.quiet:
        print(f"{report['operations']} operations on {report['paths']} paths from "
              f"{report['entries']} requests; {len(report['flags'])} low-confidence findings.")
        for path in written:
            print(f"wrote {path.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

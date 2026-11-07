#!/usr/bin/env python3
"""contract: check live traffic against a spec, or compare two specs for breaking changes.

  contract.py validate --spec api/openapi.json --har new.har
      Every recorded response is checked against the spec: missing required fields, type
      changes, null where not nullable (breaking); unknown endpoints, undocumented status codes,
      values outside an enum (warning); new undocumented fields (info).

  contract.py diff old.json new.json
      Removed endpoints, removed or retyped response fields, new required request fields or
      parameters (breaking); enum or format changes that may break strict clients (warning);
      additions (info).

--fail-on breaking|warning exits 1 when a finding at that level or worse exists, for CI.
Standard library only, deterministic output.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
LEVELS = ("breaking", "warning", "info")
STATIC_MIME = ("image/", "font/", "audio/", "video/", "text/css", "text/html")


# --------------------------------------------------------------------------- shared

def resolve(spec: dict, schema, depth: int = 0):
    while isinstance(schema, dict) and "$ref" in schema and depth < 20:
        node = spec
        for part in str(schema["$ref"]).lstrip("#/").split("/"):
            node = node.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(node, dict) else {}
        schema, depth = node, depth + 1
    return schema if isinstance(schema, dict) else {}


def schema_type(schema: dict):
    if schema.get("type"):
        return schema["type"]
    if "properties" in schema or "additionalProperties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


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
    return "array" if isinstance(value, list) else "object"


def operations(spec: dict) -> dict:
    """{(METHOD, normalized_path): (path, operation)}; parameter names normalized to {}."""
    out = {}
    for path, item in (spec.get("paths") or {}).items():
        for method in METHODS:
            op = (item or {}).get(method)
            if isinstance(op, dict):
                out[(method.upper(), re.sub(r"\{[^}]+\}", "{}", path))] = (path, op, item)
    return out


def route_regex(path: str):
    pattern = "".join("([^/]+)" if p.startswith("{") else re.escape(p)
                      for p in re.split(r"(\{[^}]+\})", path) if p)
    return re.compile("^" + pattern + "/?$")


class Findings:
    def __init__(self):
        self.items = Counter()

    def add(self, level: str, code: str, where: str, pointer: str, message: str) -> None:
        self.items[(level, code, where, pointer, message)] += 1

    def rows(self) -> list:
        rows = [{"level": l, "code": c, "where": w, "pointer": p, "message": m, "count": n}
                for (l, c, w, p, m), n in self.items.items()]
        return sorted(rows, key=lambda r: (LEVELS.index(r["level"]), r["where"], r["pointer"], r["code"]))


def summary_counts(rows: list) -> dict:
    counts = Counter(r["level"] for r in rows)
    return {level: counts.get(level, 0) for level in LEVELS}


# --------------------------------------------------------------------------- validate

def check_value(spec: dict, schema, value, pointer: str, where: str, out: Findings) -> None:
    schema = resolve(spec, schema)
    if not schema:
        return
    if value is None:
        if not schema.get("nullable") and schema_type(schema) is not None:
            out.add("breaking", "null-not-allowed", where, pointer,
                    f"got null; spec says {schema_type(schema)} and not nullable")
        return
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            for option in schema[key]:
                trial = Findings()
                check_value(spec, option, value, pointer, where, trial)
                if not any(k[0] == "breaking" for k in trial.items):
                    return
            out.add("breaking", "type-mismatch", where, pointer,
                    f"value matches none of the {key} options")
            return
    expected = schema_type(schema)
    actual = json_type(value)
    if expected and expected != actual and not (expected == "number" and actual == "integer"):
        out.add("breaking", "type-mismatch", where, pointer, f"expected {expected}, got {actual}")
        return
    if schema.get("enum") and value not in schema["enum"]:
        out.add("warning", "enum-value", where, pointer,
                f"value {json.dumps(value)} is not in the enum")
    if actual == "object":
        props = schema.get("properties") or {}
        for name in sorted(schema.get("required") or []):
            if name not in value:
                out.add("breaking", "missing-required", where, f"{pointer}.{name}",
                        "required field is missing")
        extra = schema.get("additionalProperties")
        for name in sorted(value):
            sub = f"{pointer}.{name}"
            if name in props:
                check_value(spec, props[name], value[name], sub, where, out)
            elif isinstance(extra, dict):
                check_value(spec, extra, value[name], sub, where, out)
            elif extra is not False:
                out.add("info", "undocumented-field", where, sub, "field is not in the spec")
            else:
                out.add("breaking", "forbidden-field", where, sub, "spec forbids extra fields")
    elif actual == "array" and isinstance(schema.get("items"), dict):
        for item in value:
            check_value(spec, schema["items"], item, pointer + "[]", where, out)


def validate(spec: dict, har: dict, hosts=()) -> dict:
    out = Findings()
    routes = []
    for (method, _), (path, op, _) in operations(spec).items():
        regex = route_regex(path)
        literal = len(re.sub(r"\{[^}]+\}", "", path))
        routes.append((path.count("{"), -literal, path, method, regex, op))
    routes.sort(key=lambda r: r[:4])
    checked = 0
    for raw in har.get("log", {}).get("entries") or []:
        req, resp = raw.get("request") or {}, raw.get("response") or {}
        parts = urlsplit(req.get("url") or "")
        if hosts and (parts.hostname or "").lower() not in {h.lower() for h in hosts}:
            continue
        content = resp.get("content") or {}
        mime = (content.get("mimeType") or "").split(";")[0].strip().lower()
        status = int(resp.get("status") or 0)
        if status == 0 or mime.startswith(STATIC_MIME) or "javascript" in mime:
            continue
        method = str(req.get("method") or "GET").upper()
        path = parts.path or "/"
        match = next((r for r in routes if r[3] == method and r[4].match(path)), None)
        if match is None:
            out.add("warning", "unknown-endpoint", f"{method} {path}", "",
                    "endpoint is not in the spec")
            continue
        checked += 1
        where = f"{method} {match[2]} {status}"
        responses = match[5].get("responses") or {}
        response = responses.get(str(status)) or responses.get(f"{str(status)[0]}XX") or responses.get("default")
        if response is None:
            out.add("warning", "undocumented-status", where, "", f"status {status} is not in the spec")
            continue
        response = resolve(spec, response)
        text = content.get("text") or ""
        if not text or status == 204:
            continue
        media = (response.get("content") or {}).get(mime)
        if media is None:
            if response.get("content"):
                out.add("warning", "undocumented-content-type", where, "",
                        f"content type {mime or '(none)'} is not in the spec")
            continue
        try:
            body = json.loads(text)
        except ValueError:
            continue
        check_value(spec, media.get("schema") or {}, body, "$", where, out)
    rows = out.rows()
    return {"command": "validate", "checked": checked, "summary": summary_counts(rows), "findings": rows}


# --------------------------------------------------------------------------- diff

def _enum_set(schema: dict):
    return set(json.dumps(v, sort_keys=True) for v in schema.get("enum") or [])


def compare_schema(old_spec, new_spec, old, new, pointer: str, where: str, direction: str,
                   out: Findings, depth: int = 0) -> None:
    """direction is 'request' (client sends) or 'response' (client reads)."""
    if depth > 30:
        return
    old, new = resolve(old_spec, old), resolve(new_spec, new)
    if not old or not new:
        return
    if any(k in old or k in new for k in ("oneOf", "anyOf", "allOf")):
        if json.dumps(old, sort_keys=True) != json.dumps(new, sort_keys=True):
            out.add("warning", "union-changed", where, pointer, "composite schema changed; review by hand")
        return
    ot, nt = schema_type(old), schema_type(new)
    if ot and nt and ot != nt:
        widened = (ot, nt) == ("integer", "number")
        narrowed = (ot, nt) == ("number", "integer")
        safe = widened if direction == "request" else narrowed
        out.add("info" if safe else "breaking", "type-changed", where, pointer, f"type {ot} -> {nt}")
        return
    if bool(old.get("nullable")) != bool(new.get("nullable")):
        became = bool(new.get("nullable"))
        breaking = became if direction == "response" else not became
        out.add("breaking" if breaking else "info", "nullable-changed", where, pointer,
                "now nullable" if became else "no longer nullable")
    if old.get("format") != new.get("format") and (old.get("format") or new.get("format")):
        out.add("warning", "format-changed", where, pointer,
                f"format {old.get('format') or 'none'} -> {new.get('format') or 'none'}")
    oe, ne = _enum_set(old), _enum_set(new)
    if oe or ne:
        added = sorted(ne - oe) if ne else []
        removed = sorted(oe - ne) if oe and ne else []
        if not ne and oe:
            out.add("info", "enum-removed", where, pointer, "enum constraint removed")
        if added:
            level = "warning" if direction == "response" else "info"
            out.add(level, "enum-values-added", where, pointer, "added " + ", ".join(added))
        if removed:
            level = "breaking" if direction == "request" else "info"
            out.add(level, "enum-values-removed", where, pointer, "removed " + ", ".join(removed))
    if (ot or nt) == "object":
        op, np_ = old.get("properties") or {}, new.get("properties") or {}
        oreq, nreq = set(old.get("required") or []), set(new.get("required") or [])
        for name in sorted(set(op) | set(np_)):
            sub = f"{pointer}.{name}"
            if name not in np_:
                level = "breaking" if direction == "response" else "warning"
                out.add(level, "field-removed", where, sub, "field removed")
            elif name not in op:
                if direction == "request" and name in nreq:
                    out.add("breaking", "required-field-added", where, sub, "new required request field")
                else:
                    out.add("info", "field-added", where, sub, "new optional field" if name not in nreq
                            else "new field")
            else:
                if name in oreq and name not in nreq:
                    out.add("breaking" if direction == "response" else "info", "required-changed",
                            where, sub, "required -> optional")
                elif name not in oreq and name in nreq:
                    out.add("breaking" if direction == "request" else "info", "required-changed",
                            where, sub, "optional -> required")
                compare_schema(old_spec, new_spec, op[name], np_[name], sub, where, direction, out, depth + 1)
    if (ot or nt) == "array" and isinstance(old.get("items"), dict) and isinstance(new.get("items"), dict):
        compare_schema(old_spec, new_spec, old["items"], new["items"], pointer + "[]", where,
                       direction, out, depth + 1)


def _params(spec: dict, op: dict, item: dict) -> dict:
    out = {}
    for raw in list(item.get("parameters") or []) + list(op.get("parameters") or []):
        p = resolve(spec, raw)
        if p.get("name") and p.get("in") != "path":
            out[(p["in"], p["name"])] = p
    return out


def _security(op: dict) -> set:
    return {json.dumps(sorted(alt), sort_keys=True) for alt in op.get("security") or []}


def diff(old: dict, new: dict) -> dict:
    out = Findings()
    old_ops, new_ops = operations(old), operations(new)
    for key in sorted(set(old_ops) | set(new_ops)):
        method = key[0]
        if key not in new_ops:
            path = old_ops[key][0]
            out.add("breaking", "endpoint-removed", f"{method} {path}", "", "endpoint removed")
            continue
        if key not in old_ops:
            path = new_ops[key][0]
            out.add("info", "endpoint-added", f"{method} {path}", "", "new endpoint")
            continue
        (opath, oop, oitem), (npath, nop, nitem) = old_ops[key], new_ops[key]
        where = f"{method} {npath}"
        if opath != npath:
            out.add("info", "path-param-renamed", where, "", f"was {opath}")
        oparams, nparams = _params(old, oop, oitem), _params(new, nop, nitem)
        for pkey in sorted(set(oparams) | set(nparams)):
            label = f"{pkey[0]} parameter {pkey[1]}"
            if pkey not in nparams:
                out.add("warning", "param-removed", where, label, "parameter removed")
            elif pkey not in oparams:
                req = bool(nparams[pkey].get("required"))
                out.add("breaking" if req else "info", "param-added", where, label,
                        "new required parameter" if req else "new optional parameter")
            else:
                o_req, n_req = bool(oparams[pkey].get("required")), bool(nparams[pkey].get("required"))
                if o_req != n_req:
                    out.add("breaking" if n_req else "info", "param-required-changed", where, label,
                            "optional -> required" if n_req else "required -> optional")
                compare_schema(old, new, oparams[pkey].get("schema") or {}, nparams[pkey].get("schema") or {},
                               label, where, "request", out)
        obody = resolve(old, oop.get("requestBody")) if oop.get("requestBody") else {}
        nbody = resolve(new, nop.get("requestBody")) if nop.get("requestBody") else {}
        if nbody and not obody:
            req = bool(nbody.get("required"))
            out.add("breaking" if req else "info", "request-body-added", where, "request body",
                    "new required request body" if req else "new optional request body")
        elif obody and not nbody:
            out.add("warning", "request-body-removed", where, "request body", "request body removed")
        elif obody and nbody:
            if not obody.get("required") and nbody.get("required"):
                out.add("breaking", "request-body-required", where, "request body", "body is now required")
            ocontent, ncontent = obody.get("content") or {}, nbody.get("content") or {}
            for mime in sorted(set(ocontent) | set(ncontent)):
                if mime not in ncontent:
                    out.add("breaking", "content-type-removed", where, "request body", f"{mime} no longer accepted")
                elif mime in ocontent:
                    compare_schema(old, new, ocontent[mime].get("schema") or {},
                                   ncontent[mime].get("schema") or {}, "$", where + " request", "request", out)
        oresp, nresp = oop.get("responses") or {}, nop.get("responses") or {}
        for status in sorted(set(oresp) | set(nresp)):
            swhere = f"{where} {status}"
            if status not in nresp:
                level = "breaking" if str(status).startswith("2") else "warning"
                out.add(level, "status-removed", swhere, "", "response status removed")
                continue
            if status not in oresp:
                out.add("info", "status-added", swhere, "", "new response status")
                continue
            ocontent = resolve(old, oresp[status]).get("content") or {}
            ncontent = resolve(new, nresp[status]).get("content") or {}
            for mime in sorted(set(ocontent) | set(ncontent)):
                if mime not in ncontent:
                    out.add("breaking", "content-type-removed", swhere, "", f"{mime} no longer returned")
                elif mime not in ocontent:
                    out.add("info", "content-type-added", swhere, "", f"{mime} now returned")
                else:
                    compare_schema(old, new, ocontent[mime].get("schema") or {},
                                   ncontent[mime].get("schema") or {}, "$", swhere, "response", out)
        osec, nsec = _security(oop), _security(nop)
        if osec != nsec:
            if not osec:
                out.add("breaking", "auth-added", where, "", "authentication is now required")
            elif nsec and osec - nsec:
                out.add("breaking", "auth-changed", where, "", "an accepted auth scheme was removed")
            else:
                out.add("info", "auth-relaxed", where, "", "more auth options accepted")
    rows = out.rows()
    return {"command": "diff", "summary": summary_counts(rows), "findings": rows}


# --------------------------------------------------------------------------- output

def render_markdown(result: dict, title: str) -> str:
    s = result["summary"]
    lines = [f"# Contract check: {result['command']}", "", title, ""]
    if "checked" in result:
        lines += [f"Responses checked against the spec: {result['checked']}", ""]
    lines += [f"**Result: {s['breaking']} breaking, {s['warning']} warning, {s['info']} info**", ""]
    if not result["findings"]:
        lines.append("No differences found.")
    for level in LEVELS:
        rows = [r for r in result["findings"] if r["level"] == level]
        if not rows:
            continue
        lines += [f"## {level.capitalize()}", "", "| Where | Field | Change | Code | Seen |",
                  "|---|---|---|---|---:|"]
        for r in rows:
            field = f"`{r['pointer']}`" if r["pointer"] else ""
            message = r["message"].replace("|", "\\|")
            lines.append(f"| `{r['where']}` | {field} | {message} | {r['code']} | {r['count']} |")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def exit_code(result: dict, fail_on: str) -> int:
    s = result["summary"]
    if fail_on == "breaking" and s["breaking"]:
        return 1
    if fail_on == "warning" and (s["breaking"] or s["warning"]):
        return 1
    return 0


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="contract.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="check a HAR's responses against a spec")
    v.add_argument("--spec", required=True)
    v.add_argument("--har", required=True)
    v.add_argument("--host", action="append", default=[], help="only check this host (repeatable)")
    d = sub.add_parser("diff", help="compare two specs for breaking changes")
    d.add_argument("old")
    d.add_argument("new")
    for p in (v, d):
        p.add_argument("--format", choices=["md", "json"], default="md")
        p.add_argument("--fail-on", choices=["breaking", "warning", "never"], default="never",
                       help="exit 1 when findings at this level (or worse) exist")
        p.add_argument("-o", "--output", help="write the report here instead of stdout")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = validate(_load(args.spec), _load(args.har), args.host)
            title = f"Spec `{Path(args.spec).name}` vs capture `{Path(args.har).name}`."
        else:
            result = diff(_load(args.old), _load(args.new))
            title = f"`{Path(args.old).name}` -> `{Path(args.new).name}`."
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = (json.dumps(result, indent=2, sort_keys=True) + "\n" if args.format == "json"
            else render_markdown(result, title))
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.write(text)
    return exit_code(result, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""gen_sdk: generate zero-dependency typed clients from an OpenAPI 3.0 JSON spec.

  <out>/ts/client.ts        TypeScript: interfaces for every schema + an async client using fetch
  <out>/python/client.py    Python: TypedDict models + a client using urllib.request

Base URL and auth are injected by the caller; the generated code never stores credentials.
Standard library only, deterministic output.
"""
from __future__ import annotations

import argparse
import json
import keyword
import re
import sys
from pathlib import Path

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
TS_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
TS_RESERVED = {
    "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do",
    "else", "enum", "export", "extends", "false", "finally", "for", "function", "if", "import", "in",
    "instanceof", "new", "null", "return", "super", "switch", "this", "throw", "true", "try",
    "typeof", "var", "void", "while", "with", "let", "static", "yield", "await",
}


# --------------------------------------------------------------------------- naming

def words(text: str) -> list:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def pascal(text: str) -> str:
    name = "".join(w[:1].upper() + w[1:] for w in words(text))
    if not name:
        return "Value"
    return "T" + name if name[0].isdigit() else name


def camel(text: str) -> str:
    name = pascal(text)
    return name[:1].lower() + name[1:]


def snake(text: str) -> str:
    name = "_".join(w.lower() for w in words(text)) or "value"
    if name[0].isdigit():
        name = "_" + name
    return name + "_" if keyword.iskeyword(name) else name


def ts_ident(text: str) -> str:
    name = camel(text)
    return name + "_" if name in TS_RESERVED else name


def ts_key(name: str) -> str:
    return name if TS_IDENT.match(name) else json.dumps(name)


def comment(text: str) -> str:
    return str(text).replace("*/", "* /").replace("\n", " ")


def py_doc(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


# --------------------------------------------------------------------------- spec walking

def resolve(spec: dict, schema):
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 20:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return {}
        node = spec
        for part in ref[2:].split("/"):
            node = node.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(node, dict) else {}
        schema, seen = node, seen + 1
    return schema if isinstance(schema, dict) else {}


def ref_name(schema) -> str:
    if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        return schema["$ref"].rsplit("/", 1)[-1]
    return ""


def pick_media(content: dict):
    if not content:
        return None, None
    for mime in sorted(content):
        if "json" in mime:
            return mime, content[mime]
    mime = sorted(content)[0]
    return mime, content[mime]


def collect_operations(spec: dict) -> list:
    ops, used = [], set()
    for path in sorted(spec.get("paths") or {}):
        item = spec["paths"][path] or {}
        shared = item.get("parameters") or []
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId") or (method + "_" + path)
            name = camel(op_id) or method
            base, n = name, 2
            while name in used:
                name, n = f"{base}{n}", n + 1
            used.add(name)
            params = {}
            for raw in list(shared) + list(op.get("parameters") or []):
                p = resolve(spec, raw)
                if p.get("name") and p.get("in") in ("path", "query"):
                    params[(p["in"], p["name"])] = p
            path_params = [params[("path", n)] for n in re.findall(r"\{([^}]+)\}", path)
                           if ("path", n) in params]
            for n in re.findall(r"\{([^}]+)\}", path):
                if ("path", n) not in params:
                    path_params.append({"name": n, "in": "path", "required": True,
                                        "schema": {"type": "string"}})
            query = [params[k] for k in sorted(params) if k[0] == "query"]
            body = resolve(spec, op.get("requestBody")) if op.get("requestBody") else {}
            body_mime, body_media = pick_media(body.get("content") or {})
            success = sorted(s for s in (op.get("responses") or {}) if str(s).startswith("2"))
            resp_schema, resp_mime, status = None, None, success[0] if success else None
            for code in success:
                resp = resolve(spec, op["responses"][code])
                mime, media = pick_media(resp.get("content") or {})
                if media is not None:
                    resp_schema, resp_mime, status = media.get("schema") or {}, mime, code
                    break
            ops.append({
                "name": name, "method": method.upper(), "path": path,
                "summary": op.get("summary") or "", "description": op.get("description") or "",
                "path_params": path_params, "query": query,
                "body_schema": (body_media or {}).get("schema") if body_media is not None else None,
                "body_mime": body_mime, "body_required": bool(body.get("required")),
                "resp_schema": resp_schema, "resp_mime": resp_mime, "status": status,
            })
    return ops


def client_name(spec: dict) -> str:
    title = (spec.get("info") or {}).get("title") or "Api"
    name = pascal(title)
    if not name.endswith("Client"):
        name += "Client"
    return name


# --------------------------------------------------------------------------- TypeScript

class TsTypes:
    def __init__(self, spec: dict):
        self.spec = spec
        self.decls = []
        self.names = set()
        self.components = {}
        for name in sorted(((spec.get("components") or {}).get("schemas") or {})):
            self.components[name] = pascal(name)
            self.names.add(pascal(name))
        for name in sorted(self.components):
            schema = spec["components"]["schemas"][name]
            self.declare(self.components[name], schema, fixed=True)

    def unique(self, hint: str) -> str:
        base = pascal(hint)
        name, n = base, 2
        while name in self.names:
            name, n = f"{base}{n}", n + 1
        self.names.add(name)
        return name

    def type_of(self, schema, hint: str) -> str:
        if not isinstance(schema, dict) or not schema:
            return "unknown"
        ref = ref_name(schema)
        if ref:
            return self.components.get(ref, "unknown")
        base = self._base(schema, hint)
        if schema.get("nullable") and base != "unknown":
            return f"{base} | null"
        return base

    def _base(self, schema: dict, hint: str) -> str:
        for key, joiner in (("oneOf", " | "), ("anyOf", " | "), ("allOf", " & ")):
            if schema.get(key):
                parts = [self.type_of(s, f"{hint}Option{i + 1}") for i, s in enumerate(schema[key])]
                return joiner.join(f"({p})" if "|" in p or "&" in p else p for p in parts)
        if schema.get("enum"):
            return " | ".join(json.dumps(v) for v in schema["enum"])
        kind = schema.get("type")
        if kind == "string":
            return "string"
        if kind in ("integer", "number"):
            return "number"
        if kind == "boolean":
            return "boolean"
        if kind == "array":
            return f"Array<{self.type_of(schema.get('items') or {}, hint + 'Item')}>"
        if kind == "object" or "properties" in schema:
            if schema.get("properties"):
                name = self.unique(hint)
                self.declare(name, schema)
                return name
            extra = schema.get("additionalProperties")
            value = self.type_of(extra, hint + "Value") if isinstance(extra, dict) else "unknown"
            return f"Record<string, {value}>"
        return "unknown"

    def declare(self, name: str, schema: dict, fixed: bool = False) -> None:
        if not schema.get("properties"):
            self.decls.append(f"export type {name} = {self.type_of_plain(schema, name)};")
            return
        required = set(schema.get("required") or [])
        lines = []
        for prop in sorted(schema["properties"]):
            sub = schema["properties"][prop]
            notes = []
            resolved = resolve(self.spec, sub)
            if resolved.get("format"):
                notes.append(f"format: {resolved['format']}")
            if resolved.get("description"):
                notes.append(comment(resolved["description"]))
            if notes:
                lines.append(f"  /** {'; '.join(notes)} */")
            optional = "" if prop in required else "?"
            lines.append(f"  {ts_key(prop)}{optional}: {self.type_of(sub, name + pascal(prop))};")
        doc = f"/** {comment(schema['description'])} */\n" if schema.get("description") else ""
        self.decls.append(doc + f"export interface {name} {{\n" + "\n".join(lines) + "\n}")

    def type_of_plain(self, schema: dict, name: str) -> str:
        base = self._base(schema, name + "Body")
        return f"{base} | null" if schema.get("nullable") and base != "unknown" else base


TS_RUNTIME = '''export interface ClientOptions {
  /** Base URL, e.g. "https://api.example.com". No trailing slash needed. */
  baseUrl: string;
  /** Headers sent with every request, e.g. { Authorization: `Bearer ${token}` }. */
  headers?: Record<string, string>;
  /** Called before every request; return extra headers such as a freshly refreshed token. */
  getHeaders?: () => Record<string, string> | Promise<Record<string, string>>;
  /** Custom fetch implementation. Defaults to the global fetch. */
  fetch?: typeof fetch;
}

/** Thrown for every non-2xx response. */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

type QueryValue = string | number | boolean | null | undefined | Array<string | number | boolean>;
'''


def ts_param_type(types: TsTypes, param: dict, hint: str) -> str:
    return types.type_of(param.get("schema") or {}, hint)


def generate_ts(spec: dict, source: str = "openapi.json") -> str:
    info = spec.get("info") or {}
    types = TsTypes(spec)
    ops = collect_operations(spec)
    methods = []
    for op in ops:
        prefix = pascal(op["name"])
        args, used = [], set()
        path_expr = op["path"]
        for p in op["path_params"]:
            ident = ts_ident(p["name"])
            while ident in used:
                ident += "_"
            used.add(ident)
            args.append(f"{ident}: {ts_param_type(types, p, prefix + pascal(p['name']))}")
            path_expr = path_expr.replace("{" + p["name"] + "}",
                                          "${encodeURIComponent(String(" + ident + "))}")
        path_expr = "`" + path_expr.replace("`", "\\`") + "`"
        body_arg = "undefined"
        if op["body_schema"] is not None:
            body_type = types.type_of(op["body_schema"], prefix + "Request")
            args.append(f"body: {body_type}" if op["body_required"] else f"body?: {body_type}")
            body_arg = "body"
        query_arg = "undefined"
        if op["query"]:
            fields = []
            for q in op["query"]:
                opt = "" if q.get("required") else "?"
                fields.append(f"{ts_key(q['name'])}{opt}: {ts_param_type(types, q, prefix + pascal(q['name']))}")
            any_required = any(q.get("required") for q in op["query"])
            default = "" if any_required else " = {}"
            args.append("query: { " + "; ".join(fields) + " }" + default)
            query_arg = "query"
        if op["resp_schema"] is not None:
            ret = types.type_of(op["resp_schema"], prefix + "Response")
        else:
            ret = "void"
        form = "true" if (op["body_mime"] or "") == "application/x-www-form-urlencoded" else "false"
        doc = [f"  /**", f"   * {op['method']} {comment(op['path'])}"]
        if op["summary"]:
            doc.append(f"   * {comment(op['summary'])}")
        if op["description"]:
            doc.append(f"   * {comment(op['description'])}")
        doc.append("   */")
        methods.append("\n".join(doc) + "\n"
                       f"  async {op['name']}({', '.join(args)}): Promise<{ret}> {{\n"
                       f"    return this.request<{ret}>(\"{op['method']}\", {path_expr}, {query_arg}, {body_arg}, {form});\n"
                       "  }")
    name = client_name(spec)
    header = (f"// Typed client for {comment(info.get('title', 'API'))} {comment(info.get('version', ''))}.\n"
              f"// Generated by gen_sdk.py from {source}. Zero dependencies. Regenerate instead of editing.\n")
    client = f'''export class {name} {{
  private readonly options: ClientOptions;

  constructor(options: ClientOptions) {{
    this.options = options;
  }}

  private async request<T>(
    method: string,
    path: string,
    query?: Record<string, QueryValue>,
    body?: unknown,
    form = false,
  ): Promise<T> {{
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query ?? {{}})) {{
      if (value === undefined || value === null) continue;
      for (const item of Array.isArray(value) ? value : [value]) params.append(key, String(item));
    }}
    const qs = params.toString();
    const url = this.options.baseUrl.replace(/\\/+$/, "") + path + (qs ? `?${{qs}}` : "");
    const extra = this.options.getHeaders ? await this.options.getHeaders() : {{}};
    const headers: Record<string, string> = {{ Accept: "application/json", ...this.options.headers, ...extra }};
    let payload: string | undefined;
    if (body !== undefined) {{
      if (form) {{
        headers["Content-Type"] = "application/x-www-form-urlencoded";
        payload = new URLSearchParams(body as Record<string, string>).toString();
      }} else {{
        headers["Content-Type"] = "application/json";
        payload = JSON.stringify(body);
      }}
    }}
    const doFetch = this.options.fetch ?? fetch;
    const response = await doFetch(url, {{ method, headers, body: payload }});
    const text = await response.text();
    let data: unknown = undefined;
    if (text) {{
      try {{
        data = JSON.parse(text);
      }} catch {{
        data = text;
      }}
    }}
    if (!response.ok) {{
      throw new ApiError(response.status, data, `${{method}} ${{path}} failed with ${{response.status}}`);
    }}
    return data as T;
  }}

{(chr(10) + chr(10)).join(methods)}
}}
'''
    parts = [header, "\n".join(d + "\n" for d in types.decls), TS_RUNTIME, client]
    return "\n".join(p.rstrip("\n") + "\n" for p in parts if p)


# --------------------------------------------------------------------------- Python

class PyTypes:
    def __init__(self, spec: dict):
        self.spec = spec
        self.decls = []
        self.names = {"ApiError", client_name(spec)}
        self.components = {}
        for name in sorted(((spec.get("components") or {}).get("schemas") or {})):
            self.components[name] = self.unique(name)
        for name in sorted(self.components):
            self.declare(self.components[name], spec["components"]["schemas"][name])

    def unique(self, hint: str) -> str:
        base = pascal(hint)
        name, n = base, 2
        while name in self.names:
            name, n = f"{base}{n}", n + 1
        self.names.add(name)
        return name

    def type_of(self, schema, hint: str) -> str:
        if not isinstance(schema, dict) or not schema:
            return "Any"
        ref = ref_name(schema)
        if ref:
            return self.components.get(ref, "Any")
        base = self._base(schema, hint)
        if schema.get("nullable") and base != "Any":
            return f"Optional[{base}]"
        return base

    def _base(self, schema: dict, hint: str) -> str:
        for key in ("oneOf", "anyOf"):
            if schema.get(key):
                parts = [self.type_of(s, f"{hint}Option{i + 1}") for i, s in enumerate(schema[key])]
                return "Union[" + ", ".join(parts) + "]" if len(parts) > 1 else parts[0]
        if schema.get("allOf"):
            return "Dict[str, Any]"
        if schema.get("enum"):
            return "Literal[" + ", ".join(json.dumps(v) if not isinstance(v, bool) else str(v)
                                          for v in schema["enum"]) + "]"
        kind = schema.get("type")
        if kind == "string":
            return "str"
        if kind == "integer":
            return "int"
        if kind == "number":
            return "float"
        if kind == "boolean":
            return "bool"
        if kind == "array":
            return f"List[{self.type_of(schema.get('items') or {}, hint + 'Item')}]"
        if kind == "object" or "properties" in schema:
            if schema.get("properties"):
                name = self.unique(hint)
                self.declare(name, schema)
                return name
            extra = schema.get("additionalProperties")
            value = self.type_of(extra, hint + "Value") if isinstance(extra, dict) else "Any"
            return f"Dict[str, {value}]"
        return "Any"

    def declare(self, name: str, schema: dict) -> None:
        props = schema.get("properties") or {}
        if not props:
            plain = {k: v for k, v in schema.items() if k != "properties"}
            alias = "Dict[str, Any]" if plain.get("type") == "object" and not plain.get(
                "additionalProperties") else self.type_of(plain, name + "Body")
            self.decls.append(f"{name} = {alias}")
            return
        required = set(schema.get("required") or [])
        fields = [(prop, self.type_of(props[prop], name + pascal(prop)), prop in required)
                  for prop in sorted(props)]
        valid = all(p.isidentifier() and not keyword.iskeyword(p) for p, _, _ in fields)
        req = [(p, t) for p, t, r in fields if r]
        opt = [(p, t) for p, t, r in fields if not r]
        doc = f'    """{py_doc(schema["description"])}"""\n' if schema.get("description") else ""
        if not valid:
            def functional(cls: str, items: list, total: bool) -> str:
                body = ", ".join(f"{json.dumps(p)}: {json.dumps(t)}" for p, t in items)
                return f"{cls} = TypedDict({json.dumps(cls)}, {{{body}}}" + ("" if total else ", total=False") + ")"
            if req and opt:
                self.decls.append(functional(f"_{name}Required", req, True) + "\n"
                                  + functional(f"_{name}Optional", opt, False) + "\n\n\n"
                                  + f"class {name}(_{name}Required, _{name}Optional):\n" + (doc or "    pass\n"))
            else:
                self.decls.append(functional(name, req or opt, bool(req)))
            return
        def cls(cls_name: str, bases: str, items: list) -> str:
            lines = [f"    {p}: {t}" for p, t in items]
            return f"class {cls_name}({bases}):\n" + doc + "\n".join(lines) + "\n"
        if req and opt:
            self.decls.append(cls(f"_{name}Required", "TypedDict", req) + "\n\n\n"
                              + cls(name, f"_{name}Required, total=False", opt))
        elif req:
            self.decls.append(cls(name, "TypedDict", req))
        else:
            self.decls.append(cls(name, "TypedDict, total=False", opt))


PY_RUNTIME = '''class ApiError(Exception):
    """Raised for every non-2xx response. `status` is the HTTP status, `body` the decoded body."""

    def __init__(self, status: int, body: Any, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _path(value: Any) -> str:
    return urllib.parse.quote(_query_value(value), safe="")
'''


def generate_python(spec: dict, source: str = "openapi.json") -> str:
    info = spec.get("info") or {}
    types = PyTypes(spec)
    ops = collect_operations(spec)
    methods = []
    for op in ops:
        prefix = pascal(op["name"])
        used = {"self"}
        positional, keyword_only, query_map = [], [], []
        path_expr = ""
        last = 0
        for match in re.finditer(r"\{([^}]+)\}", op["path"]):
            path_expr += op["path"][last:match.start()].replace("\\", "\\\\").replace('"', '\\"').replace("{", "{{").replace("}", "}}")
            raw = match.group(1)
            param = next((p for p in op["path_params"] if p["name"] == raw), {"schema": {}})
            ident = snake(raw)
            while ident in used:
                ident += "_"
            used.add(ident)
            positional.append(f"{ident}: {types.type_of(param.get('schema') or {}, prefix + pascal(raw))}")
            path_expr += "{_path(" + ident + ")}"
            last = match.end()
        path_expr += op["path"][last:].replace("\\", "\\\\").replace('"', '\\"').replace("{", "{{").replace("}", "}}")
        body_arg = "None"
        if op["body_schema"] is not None:
            body_type = types.type_of(op["body_schema"], prefix + "Request")
            ident = "body" if "body" not in used else "body_"
            used.add(ident)
            positional.append(f"{ident}: {body_type}" if op["body_required"]
                              else f"{ident}: Optional[{body_type}] = None")
            body_arg = ident
        for q in op["query"]:
            ident = snake(q["name"])
            while ident in used:
                ident += "_"
            used.add(ident)
            qtype = types.type_of(q.get("schema") or {}, prefix + pascal(q["name"]))
            if q.get("required"):
                keyword_only.append(f"{ident}: {qtype}")
            else:
                keyword_only.append(f"{ident}: Optional[{qtype}] = None")
            query_map.append(f"{json.dumps(q['name'])}: {ident}")
        params = ["self"] + positional + (["*"] + keyword_only if keyword_only else [])
        ret = types.type_of(op["resp_schema"], prefix + "Response") if op["resp_schema"] is not None else "None"
        query_arg = "{" + ", ".join(query_map) + "}" if query_map else "None"
        form = "True" if (op["body_mime"] or "") == "application/x-www-form-urlencoded" else "False"
        doc = [f"{op['method']} {op['path']}"]
        if op["summary"]:
            doc.append(op["summary"])
        if op["description"]:
            doc.append(op["description"])
        call = f'self._request("{op["method"]}", f"{path_expr}", {query_arg}, {body_arg}, {form})'
        body = f"        {'return ' if ret != 'None' else ''}{call}"
        methods.append(f"    def {snake(op['name'])}({', '.join(params)}) -> {ret}:\n"
                       f'        """{py_doc(chr(10).join(doc).replace(chr(10), chr(10) + chr(10) + "        "))}"""\n'
                       + body)
    name = client_name(spec)
    header = (f'"""Typed client for {py_doc(info.get("title", "API"))} {py_doc(info.get("version", ""))}.\n\n'
              f"Generated by gen_sdk.py from {source}. Zero dependencies: standard library only.\n"
              'Regenerate instead of editing by hand.\n"""\n'
              "from __future__ import annotations\n\n"
              "import json\nimport urllib.error\nimport urllib.parse\nimport urllib.request\n"
              "from typing import Any, Dict, List, Literal, Optional, TypedDict, Union\n")
    client = f'''class {name}:
    """Client for {py_doc(info.get("title", "the API"))}. Pass auth as headers, e.g.
    {name}("https://api.example.com", headers={{"Authorization": "Bearer <token>"}}).
    """

    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {{}})
        self.timeout = timeout

    def _request(self, method: str, path: str, query: Optional[Dict[str, Any]] = None,
                 body: Any = None, form: bool = False) -> Any:
        url = self.base_url + path
        pairs = []
        for key, value in (query or {{}}).items():
            if value is None:
                continue
            for item in value if isinstance(value, (list, tuple)) else [value]:
                pairs.append((key, _query_value(item)))
        if pairs:
            url += "?" + urllib.parse.urlencode(pairs)
        headers = {{"Accept": "application/json", **self.headers}}
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return _decode(response.read())
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, _decode(exc.read()), f"{{method}} {{path}} failed with {{exc.code}}") from None

{(chr(10) + chr(10)).join(methods)}
'''
    decls = "\n\n\n".join(d.rstrip("\n") for d in types.decls)
    sections = [header, decls, PY_RUNTIME, client]
    return "\n\n\n".join(s.strip("\n") for s in sections if s.strip()) + "\n"


# --------------------------------------------------------------------------- CLI

def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="gen_sdk.py",
        description="Generate zero-dependency typed TypeScript and Python clients from an OpenAPI JSON spec.")
    parser.add_argument("spec", help="OpenAPI 3.0 spec in JSON (e.g. api/openapi.json)")
    parser.add_argument("--out", default="api/sdk", help="output folder (default: api/sdk)")
    parser.add_argument("--lang", choices=["ts", "python", "all"], default="all")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.spec)
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict) or not spec.get("paths"):
        print("error: spec has no paths", file=sys.stderr)
        return 2
    out = Path(args.out)
    written = []
    if args.lang in ("ts", "all"):
        write_text(out / "ts" / "client.ts", generate_ts(spec, path.name))
        written.append(out / "ts" / "client.ts")
    if args.lang in ("python", "all"):
        write_text(out / "python" / "client.py", generate_python(spec, path.name))
        written.append(out / "python" / "client.py")
    if not args.quiet:
        print(f"{len(collect_operations(spec))} operations")
        for item in written:
            print(f"wrote {item.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

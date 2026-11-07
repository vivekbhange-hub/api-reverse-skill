#!/usr/bin/env python3
"""mock_server: serve recorded API responses from a scrubbed HAR or from spec examples.

Requests are matched by path template (from the spec when given, otherwise numeric, UUID, hex
and date segments are generalized). The closest recording wins: same path and query, then same
path, then the first 2xx recorded for the route. Unknown routes return 404 JSON listing every
known route. --latency-ms, --error-rate and --seed make slow or flaky backends reproducible.

Standard library only (http.server). It replays recordings; it keeps no state between requests.
"""
from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
STATIC_MIME = ("image/", "font/", "audio/", "video/", "text/css", "text/html")
GENERIC_SEGMENT = re.compile(
    r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|"
    r"\d{4}-\d{2}-\d{2}|(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,})$")
CORS = {"Access-Control-Allow-Origin": "*"}


class Route:
    def __init__(self, method: str, template: str):
        self.method = method.upper()
        self.template = template
        pattern = ""
        params = 0
        for part in re.split(r"(\{[^}]+\})", template):
            if part.startswith("{") and part.endswith("}"):
                pattern += "([^/]+)"
                params += 1
            else:
                pattern += re.escape(part)
        self.regex = re.compile("^" + pattern + "/?$")
        self.params = params
        self.recordings = []
        self.example = None

    @property
    def sort_key(self):
        return (self.params, -len(self.template), self.template, self.method)

    def label(self) -> str:
        return f"{self.method} {self.template}"


def _generalize(path: str) -> str:
    parts = path.split("/")
    return "/".join("{param}" if GENERIC_SEGMENT.match(p) else p for p in parts)


def _query_key(query: str) -> tuple:
    """Order-insensitive form of a query string, so ?a=1&b=2 matches ?b=2&a=1."""
    return tuple(sorted(parse_qsl(query, keep_blank_values=True)))


def _content_text(content: dict) -> str:
    text = content.get("text") or ""
    if text and content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            text = ""
    return text


class Mock:
    """Route table plus response selection. `handle` is pure apart from the seeded RNG."""

    def __init__(self, routes: list, error_rate: float = 0.0, seed: int = 0):
        self.routes = sorted(routes, key=lambda r: r.sort_key)
        self.error_rate = error_rate
        self.rng = random.Random(seed)
        self.lock = threading.Lock()

    @classmethod
    def build(cls, har=None, spec=None, error_rate: float = 0.0, seed: int = 0) -> "Mock":
        routes = {}
        if spec:
            for path in sorted(spec.get("paths") or {}):
                for method in METHODS:
                    op = (spec["paths"][path] or {}).get(method)
                    if not isinstance(op, dict):
                        continue
                    route = routes.setdefault((method.upper(), path), Route(method, path))
                    route.example = _spec_example(op)
        if har:
            ordered = sorted(routes.values(), key=lambda r: r.sort_key)
            for raw in har.get("log", {}).get("entries") or []:
                req, resp = raw.get("request") or {}, raw.get("response") or {}
                content = resp.get("content") or {}
                mime = (content.get("mimeType") or "").split(";")[0].strip().lower()
                status = int(resp.get("status") or 0)
                if status == 0 or mime.startswith(STATIC_MIME) or "javascript" in mime:
                    continue
                parts = urlsplit(req.get("url") or "")
                method = str(req.get("method") or "GET").upper()
                path = parts.path or "/"
                route = next((r for r in ordered if r.method == method and r.regex.match(path)), None)
                if route is None:
                    if spec:
                        continue
                    template = _generalize(path)
                    route = routes.setdefault((method, template), Route(method, template))
                route.recordings.append({
                    "path": path, "query": _query_key(parts.query), "status": status,
                    "mime": mime or "application/json", "body": _content_text(content).encode("utf-8"),
                })
        return cls(list(routes.values()), error_rate, seed)

    def known(self) -> list:
        return sorted(r.label() for r in self.routes)

    def handle(self, method: str, target: str) -> tuple:
        """Return (status, headers, body bytes) for one request."""
        method = method.upper()
        parts = urlsplit(target)
        path, query = parts.path or "/", _query_key(parts.query)
        with self.lock:
            roll = self.rng.random()
        if method == "OPTIONS":
            return 204, dict(CORS, **{"Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE",
                                      "Access-Control-Allow-Headers": "*"}), b""
        matching = [r for r in self.routes if r.regex.match(path)]
        if not matching:
            return _json(404, {"error": "no recorded route for this path", "method": method,
                               "path": path, "known_routes": self.known()})
        lookup = "GET" if method == "HEAD" else method
        route = next((r for r in matching if r.method == lookup), None)
        if route is None:
            allowed = sorted({r.method for r in matching})
            status, headers, body = _json(405, {"error": "method not recorded for this path",
                                                "method": method, "path": path, "allowed": allowed})
            headers["Allow"] = ", ".join(allowed)
            return status, headers, body
        if self.error_rate and roll < self.error_rate:
            return _json(500, {"error": "injected by mock_server --error-rate"}, route.label())
        rec = self._pick(route, path, query)
        if rec is not None:
            headers = dict(CORS, **{"X-Mock-Source": "har", "X-Mock-Route": route.label()})
            if rec["body"] and rec["status"] != 204:
                headers["Content-Type"] = rec["mime"]
                return rec["status"], headers, rec["body"]
            return rec["status"], headers, b""
        if route.example is not None:
            status, example = route.example
            headers = dict(CORS, **{"X-Mock-Source": "spec", "X-Mock-Route": route.label()})
            if example is None:
                return status, headers, b""
            headers["Content-Type"] = "application/json"
            return status, headers, json.dumps(example, ensure_ascii=False).encode("utf-8")
        return _json(501, {"error": "route is in the spec but has no recording or example",
                           "route": route.label()}, route.label())

    @staticmethod
    def _pick(route: Route, path: str, query: str):
        recs = route.recordings
        if not recs:
            return None
        for test in (lambda r: r["path"] == path and r["query"] == query,
                     lambda r: r["path"] == path,
                     lambda r: 200 <= r["status"] < 300):
            found = next((r for r in recs if test(r)), None)
            if found is not None:
                return found
        return recs[0]


def _spec_example(op: dict):
    responses = op.get("responses") or {}
    codes = sorted(c for c in responses if str(c).startswith("2")) or sorted(responses)
    for code in codes:
        content = (responses[code] or {}).get("content") or {}
        for mime in sorted(content):
            media = content[mime] or {}
            if "example" in media:
                return int(code), media["example"]
        if not content and str(code).isdigit():
            return int(code), None
    return None


def _json(status: int, payload: dict, route: str = "") -> tuple:
    headers = dict(CORS, **{"Content-Type": "application/json", "X-Mock-Source": "mock"})
    if route:
        headers["X-Mock-Route"] = route
    return status, headers, json.dumps(payload, indent=2).encode("utf-8")


def make_server(mock: Mock, host: str = "127.0.0.1", port: int = 0, latency_ms: int = 0,
                quiet: bool = False) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "apirev-mock/1.0"

        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if latency_ms:
                time.sleep(latency_ms / 1000.0)
            status, headers, body = mock.handle(self.command, self.path)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _serve

        def log_message(self, fmt, *args):
            if not quiet:
                sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mock_server.py",
        description="Serve recorded responses from a scrubbed HAR and/or OpenAPI spec examples.")
    parser.add_argument("--har", help="scrubbed HAR with recorded responses")
    parser.add_argument("--spec", help="OpenAPI JSON: route templates and fallback examples")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="port (default: 8080, 0 = any free)")
    parser.add_argument("--latency-ms", type=int, default=0, help="delay every response")
    parser.add_argument("--error-rate", type=float, default=0.0,
                        help="fraction of matched requests answered with 500 (0..1)")
    parser.add_argument("--seed", type=int, default=0, help="seed for --error-rate (deterministic)")
    parser.add_argument("--list", action="store_true", help="print the route table and exit")
    parser.add_argument("--quiet", action="store_true", help="do not log requests")
    args = parser.parse_args(argv)
    if not args.har and not args.spec:
        parser.error("give --har, --spec, or both")
    if not 0.0 <= args.error_rate <= 1.0:
        parser.error("--error-rate must be between 0 and 1")
    try:
        har = json.loads(Path(args.har).read_text(encoding="utf-8-sig")) if args.har else None
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8")) if args.spec else None
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    mock = Mock.build(har, spec, args.error_rate, args.seed)
    if args.list:
        for route in mock.routes:
            source = "har" if route.recordings else "spec" if route.example else "none"
            print(f"{route.label()}  ({len(route.recordings)} recordings, source: {source})")
        return 0
    server = make_server(mock, args.host, args.port, args.latency_ms, args.quiet)
    host, port = server.server_address[:2]
    print(f"Mock API on http://{host}:{port} with {len(mock.routes)} routes. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

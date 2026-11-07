"""Load helper scripts by path (skill folders have hyphens, so they are not packages)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(skill: str, script: str):
    """Import skills/<skill>/<script>.py as a module named <script>_under_test."""
    return load_path(ROOT / "skills" / skill / f"{script}.py", f"{script}_under_test")


def load_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def entry(method: str, url: str, status: int = 200, body=None, req_body=None, headers=(),
          mime: str = "application/json", req_mime: str = "application/json", resp_headers=(),
          resource_type: str = "fetch", text=None) -> dict:
    """Build one HAR entry. `body`/`req_body` are JSON-encoded unless `text`/a str is given."""
    request = {"method": method, "url": url,
               "headers": [{"name": n, "value": v} for n, v in headers], "cookies": []}
    if req_body is not None:
        request["postData"] = {"mimeType": req_mime,
                               "text": req_body if isinstance(req_body, str) else json.dumps(req_body)}
    if text is None:
        text = "" if body is None else json.dumps(body)
    response = {"status": status, "headers": [{"name": n, "value": v} for n, v in resp_headers],
                "cookies": [], "content": {"mimeType": mime if text else "", "text": text}}
    return {"request": request, "response": response, "_resourceType": resource_type}


def har(*entries) -> dict:
    return {"log": {"version": "1.2", "creator": {"name": "test", "version": "1"}, "entries": list(entries)}}

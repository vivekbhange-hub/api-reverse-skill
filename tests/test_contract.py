"""Tests for skills/apirev-contract/contract.py (validate and diff)."""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from _load import ROOT, entry, har, load

contract = load("apirev-contract", "contract")
API = "https://api.shop.example"
OUTPUT = ROOT / "examples" / "output"

PET = {"type": "object", "required": ["id", "name"], "properties": {
    "id": {"type": "integer"}, "name": {"type": "string"},
    "species": {"type": "string", "enum": ["cat", "dog"]},
    "chip": {"type": "string", "nullable": True},
    "weight": {"type": "number"},
    "tags": {"type": "array", "items": {"type": "string"}}}}
SPEC = {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": {
    "/pets/{petId}": {"get": {"responses": {
        "200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}}}},
    "/pets": {
        "get": {"parameters": [{"name": "page", "in": "query", "required": False, "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "OK", "content": {"application/json": {
                    "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}}}}},
        "post": {"requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "required": ["name"], "properties": {"name": {"type": "string"},
                                                                   "species": {"type": "string", "enum": ["cat", "dog"]}}}}}},
                 "responses": {"201": {"description": "Created"}}}}},
    "components": {"schemas": {"Pet": PET}}}


def codes(result, level=None):
    return sorted((r["code"], r["pointer"]) for r in result["findings"] if level is None or r["level"] == level)


class ValidateTests(unittest.TestCase):
    def test_clean_traffic(self):
        result = contract.validate(SPEC, har(
            entry("GET", API + "/pets/1", body={"id": 1, "name": "A", "chip": None, "weight": 3}),
            entry("GET", API + "/pets", body=[{"id": 1, "name": "A", "tags": ["x"]}])))
        self.assertEqual(result["summary"], {"breaking": 0, "warning": 0, "info": 0})
        self.assertEqual(result["checked"], 2)

    def test_breaking_findings(self):
        result = contract.validate(SPEC, har(
            entry("GET", API + "/pets/1", body={"id": "1", "weight": 2.5, "species": "cat"}),
            entry("GET", API + "/pets/2", body={"id": 2, "name": None}),
            entry("GET", API + "/pets", body=[{"id": 1, "name": "A", "tags": [1]}])))
        self.assertEqual(codes(result, "breaking"), [
            ("missing-required", "$.name"), ("null-not-allowed", "$.name"),
            ("type-mismatch", "$.id"), ("type-mismatch", "$[].tags[]")])

    def test_warnings_and_info(self):
        result = contract.validate(SPEC, har(
            entry("GET", API + "/pets/1", body={"id": 1, "name": "A", "species": "bird", "color": "red"}),
            entry("GET", API + "/pets/2", 500, body={"error": "x"}),
            entry("GET", API + "/owners/1", body={}),
            entry("GET", API + "/logo.png", text="x", mime="image/png")))
        self.assertEqual(codes(result, "warning"), [("enum-value", "$.species"), ("undocumented-status", ""),
                                                    ("unknown-endpoint", "")])
        self.assertEqual(codes(result, "info"), [("undocumented-field", "$.color")])

    def test_repeated_findings_are_counted(self):
        result = contract.validate(SPEC, har(*[entry("GET", API + f"/pets/{i}", body={"id": i}) for i in range(3)]))
        self.assertEqual(result["findings"][0]["count"], 3)

    def test_demo_capture_matches_demo_spec(self):
        spec = json.loads((OUTPUT / "openapi.json").read_text(encoding="utf-8"))
        capture = json.loads((OUTPUT / "capture.scrubbed.har").read_text(encoding="utf-8"))
        result = contract.validate(spec, capture)
        self.assertEqual(result["summary"], {"breaking": 0, "warning": 0, "info": 0})


class DiffTests(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(contract.diff(SPEC, copy.deepcopy(SPEC))["findings"], [])

    def test_breaking_changes(self):
        new = copy.deepcopy(SPEC)
        del new["paths"]["/pets/{petId}"]
        pet = new["components"]["schemas"]["Pet"]
        del pet["properties"]["name"]
        pet["properties"]["weight"]["type"] = "string"
        pet["properties"]["chip"]["nullable"] = False
        pet["required"] = ["id"]
        body = new["paths"]["/pets"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        body["properties"]["owner"] = {"type": "string"}
        body["required"].append("owner")
        body["properties"]["species"]["enum"] = ["cat"]
        new["paths"]["/pets"]["get"]["parameters"][0]["required"] = True
        result = contract.diff(SPEC, new)
        self.assertEqual(codes(result, "breaking"), [
            ("endpoint-removed", ""), ("enum-values-removed", "$.species"),
            ("field-removed", "$[].name"), ("param-required-changed", "query parameter page"),
            ("required-field-added", "$.owner"), ("type-changed", "$[].weight")])
        self.assertIn(("nullable-changed", "$[].chip"), codes(result, "info"))

    def test_non_breaking_changes(self):
        new = copy.deepcopy(SPEC)
        new["paths"]["/owners"] = {"get": {"responses": {"200": {"description": "OK"}}}}
        new["components"]["schemas"]["Pet"]["properties"]["color"] = {"type": "string"}
        new["paths"]["/pets"]["get"]["parameters"].append({"name": "q", "in": "query", "schema": {"type": "string"}})
        new["paths"]["/pets/{id}"] = new["paths"].pop("/pets/{petId}")
        result = contract.diff(SPEC, new)
        self.assertEqual(result["summary"]["breaking"], 0)
        self.assertIn(("endpoint-added", ""), codes(result, "info"))
        self.assertIn(("path-param-renamed", ""), codes(result, "info"))
        self.assertIn(("param-added", "query parameter q"), codes(result, "info"))
        self.assertIn(("field-added", "$.color"), codes(result, "info"))

    def test_response_enum_growth_is_a_warning(self):
        new = copy.deepcopy(SPEC)
        new["components"]["schemas"]["Pet"]["properties"]["species"]["enum"] = ["cat", "dog", "bird"]
        self.assertIn(("enum-values-added", "$.species"), codes(contract.diff(SPEC, new), "warning"))

    def test_type_widening_direction(self):
        old = {"paths": {"/a": {"post": {
            "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {"n": {"type": "integer"}}}}}},
            "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"n": {"type": "integer"}}}}}}}}}}}
        new = json.loads(json.dumps(old).replace('"integer"', '"number"'))
        result = contract.diff(old, new)
        levels = {(r["where"], r["level"]) for r in result["findings"]}
        self.assertEqual(levels, {("POST /a request", "info"), ("POST /a 200", "breaking")})

    def test_auth_changes(self):
        new = copy.deepcopy(SPEC)
        new["paths"]["/pets"]["get"]["security"] = [{"bearerAuth": []}]
        self.assertIn(("auth-added", ""), codes(contract.diff(SPEC, new), "breaking"))


class CliTests(unittest.TestCase):
    def test_exit_codes_and_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old, new = tmp / "old.json", tmp / "new.json"
            old.write_text(json.dumps(SPEC), encoding="utf-8")
            changed = copy.deepcopy(SPEC)
            del changed["paths"]["/pets/{petId}"]
            new.write_text(json.dumps(changed), encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(contract.main(["diff", str(old), str(new)]), 0)
            self.assertIn("**Result: 1 breaking, 0 warning, 0 info**", out.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(contract.main(["diff", str(old), str(new), "--fail-on", "breaking"]), 1)
                self.assertEqual(contract.main(["diff", str(old), str(old), "--fail-on", "warning"]), 0)
            self.assertEqual(contract.main(["diff", str(old), str(new), "--format", "json",
                                            "-o", str(tmp / "r.json")]), 0)
            self.assertEqual(json.loads((tmp / "r.json").read_text(encoding="utf-8"))["summary"]["breaking"], 1)
            capture = tmp / "c.har"
            capture.write_text(json.dumps(har(entry("GET", API + "/owners", body={}))), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(contract.main(["validate", "--spec", str(old), "--har", str(capture),
                                                "--fail-on", "warning"]), 1)
                self.assertEqual(contract.main(["validate", "--spec", str(old), "--har", str(capture),
                                                "--fail-on", "breaking"]), 0)


if __name__ == "__main__":
    unittest.main()

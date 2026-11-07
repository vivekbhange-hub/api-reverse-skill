"""Tests for skills/apirev-sdk/gen_sdk.py: generated code must be valid and typed."""
import io
import json
import re
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from _load import ROOT, load

gen = load("apirev-sdk", "gen_sdk")
DEMO_SPEC = json.loads((ROOT / "examples" / "output" / "openapi.json").read_text(encoding="utf-8"))

SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Tricky Shop", "version": "2.0.0"},
    "paths": {
        "/items/{itemId}": {
            "get": {
                "operationId": "getItem",
                "summary": 'Get one item """ with quotes */',
                "parameters": [{"name": "itemId", "in": "path", "required": True, "schema": {"type": "integer"}},
                               {"name": "class", "in": "query", "schema": {"type": "string", "enum": ["a", "b"]}},
                               {"name": "tags", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}}],
                "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/Item"}}}}},
            },
            "delete": {"operationId": "deleteItem", "responses": {"204": {"description": "No Content"}}},
        },
        "/items": {
            "post": {
                "operationId": "create-item",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["name"],
                    "properties": {"name": {"type": "string"}, "content-type": {"type": "string"},
                                   "from": {"type": "integer", "nullable": True}}}}}},
                "responses": {"201": {"description": "Created", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"id": {"type": "integer"},
                                                     "meta": {"type": "object", "additionalProperties": {"type": "number"}},
                                                     "value": {"oneOf": [{"type": "string"}, {"type": "integer"}]}}}}}}},
            },
        },
        "/login": {"post": {"operationId": "login", "requestBody": {"content": {
            "application/x-www-form-urlencoded": {"schema": {"type": "object", "properties": {"user": {"type": "string"}}}}}},
            "responses": {"200": {"description": "OK"}}}},
    },
    "components": {"schemas": {"Item": {"type": "object", "required": ["id"], "description": "An item.",
                                        "properties": {"id": {"type": "integer"}, "name": {"type": "string", "format": "uuid"},
                                                       "owner": {"type": "object", "properties": {"name": {"type": "string"}}}}}}},
}


def balanced(text: str) -> bool:
    pairs, stack = {")": "(", "]": "[", "}": "{"}, []
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    for ch in re.sub(r'"(?:\\.|[^"\\])*"', '""', text):
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def py_module(source: str):
    module = types.ModuleType("generated_client")
    exec(compile(source, "client.py", "exec"), module.__dict__)
    return module


class PythonTests(unittest.TestCase):
    def test_demo_client_compiles_and_has_methods(self):
        source = gen.generate_python(DEMO_SPEC)
        module = py_module(source)
        client = module.PetShelterAPIClient("https://api.petshelter.example/")
        self.assertEqual(client.base_url, "https://api.petshelter.example")
        for name in ("list_pets", "get_pet", "create_pet", "update_pet", "delete_pet", "get_adoption",
                     "get_shelter", "post_auth_login"):
            self.assertTrue(callable(getattr(client, name)), name)
        self.assertTrue(issubclass(module.ApiError, Exception))
        self.assertIn("def list_pets(self, *, limit: int, page: int, species: Optional[str] = None)", source)
        self.assertIn("-> None:", source)
        self.assertNotIn("\r\n", source)

    def test_tricky_spec(self):
        source = gen.generate_python(SPEC)
        module = py_module(source)
        self.assertIn("class Item(_ItemRequired, total=False):", source)
        self.assertIn('Literal["a", "b"]', source)
        self.assertIn("class_: Optional[", source)
        self.assertIn("tags: Optional[List[str]]", source)
        self.assertIn('TypedDict("_CreateItemRequestOptional", {"content-type": "str", "from": "Optional[int]"}', source)
        self.assertIn("Dict[str, float]", source)
        self.assertIn("Union[str, int]", source)
        self.assertIn("form: bool = False", source)
        self.assertIn('self._request("POST", f"/login", None, body, True)', source)
        self.assertIn('\\"\\"\\"', source)
        self.assertEqual(module.Item.__required_keys__, frozenset({"id"}))

    def test_query_and_path_helpers(self):
        module = py_module(gen.generate_python(SPEC))
        self.assertEqual(module._query_value(True), "true")
        self.assertEqual(module._path("a/b c"), "a%2Fb%20c")


class TypeScriptTests(unittest.TestCase):
    def test_demo_client_structure(self):
        source = gen.generate_ts(DEMO_SPEC)
        self.assertTrue(balanced(source))
        self.assertIn("export class PetShelterAPIClient {", source)
        self.assertIn("export class ApiError extends Error {", source)
        self.assertIn("export interface ClientOptions {", source)
        for name in ("listPets", "getPet", "createPet", "updatePet", "deletePet", "getAdoption", "getShelter"):
            self.assertRegex(source, rf"async {name}\(")
        self.assertIn("async getPet(petId: number): Promise<GetPetResponse>", source)
        self.assertIn("async deletePet(petId: number): Promise<void>", source)
        self.assertIn('species: "cat" | "dog" | "rabbit";', source)
        self.assertIn("microchip_id: string | null;", source)
        self.assertNotIn("undefined;", source.split("export class")[0])

    def test_tricky_spec(self):
        source = gen.generate_ts(SPEC)
        self.assertTrue(balanced(source))
        self.assertIn('"content-type"?: string;', source)
        self.assertIn("from?: number | null;", source)
        self.assertIn("export interface Item {", source)
        self.assertIn("Record<string, number>", source)
        self.assertIn('query: { class?: "a" | "b"; tags?: Array<string> } = {}', source)
        self.assertIn("Array<string>", source)
        self.assertIn("* /", source)
        self.assertIn("async createItem(body: CreateItemRequest): Promise<CreateItemResponse>", source)
        self.assertIn('"POST", `/login`, undefined, body, true', source)
        identifiers = re.findall(r"export (?:interface|class|type) (\w+)", source)
        self.assertEqual(len(identifiers), len(set(identifiers)))


class CliTests(unittest.TestCase):
    def test_cli_writes_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = Path(tmp) / "openapi.json"
            spec_path.write_text(json.dumps(SPEC), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(gen.main([str(spec_path), "--out", str(Path(tmp) / "sdk")]), 0)
            self.assertTrue((Path(tmp) / "sdk" / "ts" / "client.ts").exists())
            self.assertTrue((Path(tmp) / "sdk" / "python" / "client.py").exists())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(gen.main([str(spec_path), "--out", str(Path(tmp) / "only"), "--lang", "python"]), 0)
            self.assertFalse((Path(tmp) / "only" / "ts").exists())

    def test_deterministic(self):
        self.assertEqual(gen.generate_ts(DEMO_SPEC), gen.generate_ts(json.loads(json.dumps(DEMO_SPEC))))
        self.assertEqual(gen.generate_python(DEMO_SPEC), gen.generate_python(DEMO_SPEC))


if __name__ == "__main__":
    unittest.main()

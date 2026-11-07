"""Tests for skills/apirev-mock/mock_server.py, including a real server on an ephemeral port."""
import io
import json
import threading
import types
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout

from _load import ROOT, entry, har, load

mock_server = load("apirev-mock", "mock_server")
gen = load("apirev-sdk", "gen_sdk")
API = "https://api.shop.example"
OUTPUT = ROOT / "examples" / "output"

SPEC = {"openapi": "3.0.3", "info": {"title": "Shop", "version": "1"}, "paths": {
    "/items": {"get": {"responses": {"200": {"description": "OK", "content": {"application/json": {
        "example": [{"id": 1}]}}}}}},
    "/items/{itemId}": {"get": {"responses": {"200": {"description": "OK", "content": {"application/json": {
        "example": {"id": 1, "from": "spec"}}}}, "404": {"description": "x"}}},
                        "delete": {"responses": {"204": {"description": "No Content"}}}},
    "/items/search": {"get": {"responses": {"200": {"description": "OK"}}}},
}}

HAR = har(
    entry("GET", API + "/items/1", body={"id": 1, "name": "one"}),
    entry("GET", API + "/items/2", body={"id": 2, "name": "two"}),
    entry("GET", API + "/items/999", 404, body={"error": "not found"}),
    entry("GET", API + "/items?page=2", body=[{"id": 3}]),
    entry("GET", API + "/items?page=1", body=[{"id": 1}]),
    entry("GET", API + "/logo.png", text="x", mime="image/png"),
)


def body(result):
    return json.loads(result[2].decode("utf-8")) if result[2] else None


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.mock = mock_server.Mock.build(HAR, SPEC)

    def test_exact_path_wins(self):
        self.assertEqual(body(self.mock.handle("GET", "/items/2")), {"id": 2, "name": "two"})
        status, headers, _ = self.mock.handle("GET", "/items/999")
        self.assertEqual((status, headers["X-Mock-Source"]), (404, "har"))

    def test_query_match_then_first_2xx(self):
        self.assertEqual(body(self.mock.handle("GET", "/items?page=1")), [{"id": 1}])
        self.assertEqual(body(self.mock.handle("GET", "/items?page=7")), [{"id": 3}])
        self.assertEqual(body(self.mock.handle("GET", "/items/55")), {"id": 1, "name": "one"})

    def test_spec_example_fallback_and_literal_priority(self):
        status, headers, raw = self.mock.handle("DELETE", "/items/5")
        self.assertEqual((status, headers["X-Mock-Source"], raw), (204, "spec", b""))
        status, headers, _ = self.mock.handle("GET", "/items/search")
        self.assertEqual(headers["X-Mock-Route"], "GET /items/search")

    def test_unknown_route_lists_known_routes(self):
        status, headers, raw = self.mock.handle("GET", "/nope")
        data = json.loads(raw)
        self.assertEqual(status, 404)
        self.assertIn("GET /items/{itemId}", data["known_routes"])
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_wrong_method(self):
        status, headers, raw = self.mock.handle("PUT", "/items/1")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "DELETE, GET")

    def test_options_and_cors(self):
        status, headers, _ = self.mock.handle("OPTIONS", "/items")
        self.assertEqual((status, headers["Access-Control-Allow-Origin"]), (204, "*"))

    def test_error_rate_is_seeded(self):
        def statuses(seed):
            m = mock_server.Mock.build(HAR, SPEC, error_rate=0.5, seed=seed)
            return [m.handle("GET", "/items/1")[0] for _ in range(20)]
        self.assertEqual(statuses(42), statuses(42))
        self.assertIn(500, statuses(42))
        self.assertIn(200, statuses(42))
        never = mock_server.Mock.build(HAR, SPEC, error_rate=0.0)
        self.assertEqual({never.handle("GET", "/items/1")[0] for _ in range(10)}, {200})

    def test_har_only_generalizes_ids(self):
        m = mock_server.Mock.build(HAR)
        self.assertIn("GET /items/{param}", m.known())
        self.assertNotIn("GET /logo.png", m.known())
        self.assertEqual(body(m.handle("GET", "/items/77")), {"id": 1, "name": "one"})

    def test_spec_only(self):
        m = mock_server.Mock.build(spec=SPEC)
        self.assertEqual(body(m.handle("GET", "/items/9")), {"id": 1, "from": "spec"})
        self.assertEqual(m.handle("GET", "/items/search")[0], 200)

    def test_list_cli(self):
        with redirect_stdout(io.StringIO()) as out:
            code = mock_server.main(["--har", str(OUTPUT / "capture.scrubbed.har"),
                                     "--spec", str(OUTPUT / "openapi.json"), "--list"])
        self.assertEqual(code, 0)
        self.assertIn("GET /v1/pets/{petId}  (5 recordings, source: har)", out.getvalue())


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scrubbed = json.loads((OUTPUT / "capture.scrubbed.har").read_text(encoding="utf-8"))
        cls.spec = json.loads((OUTPUT / "openapi.json").read_text(encoding="utf-8"))
        mock = mock_server.Mock.build(scrubbed, cls.spec)
        cls.server = mock_server.make_server(mock, "127.0.0.1", 0, latency_ms=1, quiet=True)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def fetch(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"null")

    def test_over_http(self):
        status, data = self.fetch("/v1/pets/3")
        self.assertEqual((status, data["name"]), (200, "Miso"))
        status, data = self.fetch("/v1/pets/999")
        self.assertEqual(status, 404)
        status, data = self.fetch("/v1/unknown")
        self.assertEqual(status, 404)
        self.assertIn("GET /v1/pets", data["known_routes"])
        status, data = self.fetch("/v1/pets/58", method="DELETE")
        self.assertEqual((status, data), (204, None))

    def test_generated_python_client_against_mock(self):
        module = types.ModuleType("client_vs_mock")
        exec(compile(gen.generate_python(self.spec), "client.py", "exec"), module.__dict__)
        client = module.PetShelterAPIClient(self.base, headers={"Authorization": "Bearer test"})
        page = client.list_pets(page=2, limit=3)
        self.assertEqual(page["page"], 2)
        self.assertEqual(client.get_shelter("east-end")["slug"], "east-end")
        self.assertIsNone(client.delete_pet(58))
        with self.assertRaises(module.ApiError) as ctx:
            client.get_pet(999)
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()

"""The bundled demo: committed outputs must be exactly what the scripts produce (examples/regenerate.py)."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from _load import ROOT, load, load_path

regen = load_path(ROOT / "examples" / "regenerate.py", "regenerate_under_test")
h2o = load("apirev-infer", "har2openapi")
OUTPUT = ROOT / "examples" / "output"
EXPECTED = ["capture.scrubbed.har", "inference-report.md", "openapi.json", "openapi.yaml",
            "sdk/python/client.py", "sdk/ts/client.ts"]


class DeterminismTests(unittest.TestCase):
    def test_regenerating_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = regen.generate(Path(tmp))
            self.assertEqual(written, EXPECTED)
            self.assertEqual(regen.compare(OUTPUT, Path(tmp)), [])

    def test_check_mode(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(regen.main(["--check"]), 0)
        self.assertIn("up to date", out.getvalue())

    def test_compare_reports_differences(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "x.txt").write_text("1", encoding="utf-8")
            (Path(b) / "x.txt").write_text("2", encoding="utf-8")
            (Path(b) / "y.txt").write_text("2", encoding="utf-8")
            self.assertEqual(regen.compare(Path(a), Path(b)), ["y.txt: not committed", "x.txt: content differs"])

    def test_outputs_use_lf(self):
        for name in EXPECTED:
            with self.subTest(file=name):
                self.assertNotIn(b"\r\n", (OUTPUT / name).read_bytes())


class DemoContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads((OUTPUT / "openapi.json").read_text(encoding="utf-8"))
        cls.raw_har = (ROOT / "examples" / "petshelter.har").read_text(encoding="utf-8")
        cls.scrubbed = (OUTPUT / "capture.scrubbed.har").read_text(encoding="utf-8")

    def test_fixture_is_fictional_and_has_secrets_to_scrub(self):
        self.assertIn("api.petshelter.example", self.raw_har)
        self.assertTrue(h2o.find_unscrubbed(json.loads(self.raw_har)))
        self.assertGreaterEqual(len(json.loads(self.raw_har)["log"]["entries"]), 25)

    def test_scrubbed_capture_is_clean(self):
        self.assertEqual(h2o.find_unscrubbed(json.loads(self.scrubbed)), [])
        for secret in ("eyJ", "correct-horse", "sk_test_", "4f9c2a7e", "@mail.example", "+1 555"):
            self.assertNotIn(secret, self.scrubbed)
        for dropped in ("logo.png", "app.css", "analytics-vendor", "<!doctype"):
            self.assertNotIn(dropped, self.scrubbed)

    def test_spec_paths_and_inference(self):
        self.assertEqual(sorted(self.spec["paths"]), [
            "/v1/adoptions", "/v1/adoptions/{adoptionId}", "/v1/auth/login", "/v1/me", "/v1/pets",
            "/v1/pets/{petId}", "/v1/shelters/{shelterSlug}", "/v1/stats/daily"])
        pets = self.spec["paths"]["/v1/pets"]["get"]
        self.assertEqual(pets["x-apirev-pagination"]["style"], "page")
        item = pets["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["data"]["items"]
        self.assertEqual(item["properties"]["species"]["enum"], ["cat", "dog", "rabbit"])
        self.assertEqual(item["properties"]["weight_kg"]["type"], "number")
        self.assertTrue(item["properties"]["microchip_id"]["nullable"])
        schemes = self.spec["components"]["securitySchemes"]
        self.assertEqual(schemes["bearerAuth"]["bearerFormat"], "JWT")
        self.assertEqual(schemes["xApiKeyHeader"]["name"], "X-API-Key")
        self.assertNotIn("REDACTED", json.dumps(schemes))

    def test_report_flags_low_confidence(self):
        report = (OUTPUT / "inference-report.md").read_text(encoding="utf-8")
        self.assertIn("slug by cardinality", report)
        self.assertIn("may be an enum but needs 5+ samples", report)
        self.assertIn("| GET | `/v1/pets/{petId}` | `getPet` | 5 | 200 (4), 404 | high |", report)


if __name__ == "__main__":
    unittest.main()

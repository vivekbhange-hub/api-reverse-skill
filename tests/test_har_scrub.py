"""Tests for skills/apirev-capture/har_scrub.py."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _load import entry, har, load

scrub = load("apirev-capture", "har_scrub")
API = "https://api.shop.example"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLXZhbHVl"


def run(*entries, **kwargs):
    return scrub.scrub_har(har(*entries), **kwargs)


def text_of(data) -> str:
    return json.dumps(data)


class HeaderTests(unittest.TestCase):
    def test_authorization_keeps_scheme(self):
        out, summary = run(entry("GET", API + "/a", body={}, headers=[("Authorization", "Bearer " + JWT)]))
        headers = out["log"]["entries"][0]["request"]["headers"]
        self.assertEqual(headers[0]["value"], "Bearer REDACTED-jwt")
        self.assertEqual(summary["redacted"]["header: authorization"], 1)
        out, _ = run(entry("GET", API + "/a", body={}, headers=[("Authorization", "Basic dXNlcjpwYXNz")]))
        self.assertEqual(out["log"]["entries"][0]["request"]["headers"][0]["value"], "Basic REDACTED")

    def test_cookies_keep_names(self):
        e = entry("GET", API + "/a", body={}, headers=[("Cookie", "sid=abc123; theme=dark")],
                  resp_headers=[("Set-Cookie", "sid=xyz; Path=/; HttpOnly")])
        e["request"]["cookies"] = [{"name": "sid", "value": "abc123"}]
        out, summary = run(e)
        req = out["log"]["entries"][0]["request"]
        self.assertEqual(req["headers"][0]["value"], "sid=REDACTED; theme=REDACTED")
        self.assertEqual(req["cookies"][0]["value"], "REDACTED")
        self.assertEqual(out["log"]["entries"][0]["response"]["headers"][0]["value"],
                         "sid=REDACTED; Path=/; HttpOnly")
        self.assertEqual(summary["redacted"]["cookie: sid"], 1)

    def test_api_key_like_headers(self):
        out, _ = run(entry("GET", API + "/a", body={}, headers=[
            ("X-API-Key", "k1"), ("X-Auth-Token", "t1"), ("X-CSRF-Token", "c1"), ("Accept", "application/json")]))
        values = [h["value"] for h in out["log"]["entries"][0]["request"]["headers"]]
        self.assertEqual(values, ["REDACTED", "REDACTED", "REDACTED", "application/json"])


class QueryAndBodyTests(unittest.TestCase):
    def test_query_tokens_in_url_and_query_string(self):
        e = entry("GET", API + "/a?access_token=abc&page=2&api_key=k", body={})
        e["request"]["queryString"] = [{"name": "access_token", "value": "abc"}, {"name": "page", "value": "2"}]
        out, _ = run(e)
        req = out["log"]["entries"][0]["request"]
        self.assertEqual(req["url"], API + "/a?access_token=REDACTED&page=2&api_key=REDACTED")
        self.assertEqual(req["queryString"][0]["value"], "REDACTED")
        self.assertEqual(req["queryString"][1]["value"], "2")

    def test_json_keys_and_value_patterns(self):
        body = {"user": {"password": "hunter2", "token_type": "Bearer", "expires_in": 3600,
                         "refresh_token": "r1", "note": "key AKIA" + "ABCDEFGHIJKLMNOP here",
                         "blob": JWT, "hash": "a" * 40, "count": 3}}
        out, summary = run(entry("POST", API + "/login", req_body=body, body=body))
        resp = json.loads(out["log"]["entries"][0]["response"]["content"]["text"])["user"]
        self.assertEqual(resp["password"], "REDACTED")
        self.assertEqual(resp["refresh_token"], "REDACTED")
        self.assertEqual(resp["token_type"], "Bearer")
        self.assertEqual(resp["expires_in"], 3600)
        self.assertEqual(resp["note"], "key REDACTED-aws-key here")
        self.assertEqual(resp["blob"], "REDACTED-jwt")
        self.assertEqual(resp["hash"], "REDACTED-hex")
        self.assertEqual(resp["count"], 3)
        req = json.loads(out["log"]["entries"][0]["request"]["postData"]["text"])["user"]
        self.assertEqual(req["password"], "REDACTED")
        self.assertGreaterEqual(summary["redacted"]["json key: password"], 2)

    def test_provider_keys_and_text_bodies(self):
        out, _ = run(entry("GET", API + "/a", text="key=sk_live_" + "abcdefgh12345678 ok", mime="text/plain"))
        self.assertEqual(out["log"]["entries"][0]["response"]["content"]["text"], "key=REDACTED-key ok")

    def test_form_bodies(self):
        out, _ = run(entry("POST", API + "/login", req_body="user=sam&password=pw1",
                           req_mime="application/x-www-form-urlencoded", body={}))
        self.assertEqual(out["log"]["entries"][0]["request"]["postData"]["text"], "user=sam&password=REDACTED")

    def test_untouched_json_keeps_formatting(self):
        e = entry("GET", API + "/a", text='{ "a": 1 }')
        out, _ = run(e)
        self.assertEqual(out["log"]["entries"][0]["response"]["content"]["text"], '{ "a": 1 }')

    def test_base64_content_is_decoded_and_scrubbed(self):
        import base64
        e = entry("GET", API + "/a", body={})
        e["response"]["content"] = {"mimeType": "application/json", "encoding": "base64",
                                    "text": base64.b64encode(json.dumps({"secret": "s"}).encode()).decode()}
        out, _ = run(e)
        content = out["log"]["entries"][0]["response"]["content"]
        self.assertNotIn("encoding", content)
        self.assertEqual(json.loads(content["text"]), {"secret": "REDACTED"})


class PiiTests(unittest.TestCase):
    BODY = {"email": "sam@mail.example", "phone": "+1 555 010 4477", "chip": "985112003456789",
            "born": "2026-11-01T10:00:00Z", "day": "2026-11-01 09:30"}

    def test_pii_reported_but_kept_without_flag(self):
        out, summary = run(entry("GET", API + "/a", body=self.BODY))
        self.assertIn("sam@mail.example", text_of(out))
        self.assertEqual(summary["pii_found"], {"email": 1, "phone": 1})
        self.assertIn("NOT replaced", scrub.format_summary(summary, "in", "out"))

    def test_pii_replaced_with_flag(self):
        out, summary = run(entry("GET", API + "/a", body=self.BODY), pii=True)
        body = json.loads(out["log"]["entries"][0]["response"]["content"]["text"])
        self.assertEqual(body["email"], "redacted@example.invalid")
        self.assertEqual(body["phone"], "REDACTED-phone")
        self.assertEqual(body["chip"], "985112003456789")
        self.assertEqual(body["born"], self.BODY["born"])
        self.assertEqual(body["day"], self.BODY["day"])


class FilterTests(unittest.TestCase):
    def test_static_and_hosts_dropped(self):
        out, summary = run(
            entry("GET", API + "/logo.png", text="x", mime="image/png"),
            entry("GET", API + "/site.css", text="x", mime="text/css"),
            entry("GET", "https://www.shop.example/", text="<html>", mime="text/html", resource_type="document"),
            entry("GET", API + "/font", text="x", mime="font/woff2"),
            entry("POST", "https://collect.tracker.example/e", 204, req_body={"e": 1}),
            entry("GET", API + "/items", body=[]),
            include_hosts=["*.shop.example"])
        urls = [e["request"]["url"] for e in out["log"]["entries"]]
        self.assertEqual(urls, [API + "/items"])
        self.assertEqual(summary["dropped_static"], {"css": 1, "font": 1, "html": 1, "image": 1})
        self.assertEqual(summary["dropped_hosts"], {"collect.tracker.example": 1})

    def test_keep_static(self):
        out, _ = run(entry("GET", API + "/logo.png", text="x", mime="image/png"), keep_static=True)
        self.assertEqual(len(out["log"]["entries"]), 1)

    def test_private_fields_removed(self):
        e = entry("GET", API + "/a", body={})
        e["_initiator"] = {"stack": "x"}
        e["serverIPAddress"] = "10.0.0.1"
        out, _ = run(e)
        self.assertNotIn("_initiator", out["log"]["entries"][0])
        self.assertNotIn("serverIPAddress", out["log"]["entries"][0])

    def test_input_not_mutated_and_idempotent(self):
        original = har(entry("GET", API + "/a?token=t", body={"password": "p"},
                             headers=[("Authorization", "Bearer " + JWT)]))
        snapshot = json.dumps(original)
        once, _ = scrub.scrub_har(original)
        self.assertEqual(json.dumps(original), snapshot)
        twice, summary = scrub.scrub_har(once)
        self.assertEqual(twice, once)
        self.assertEqual(summary["redacted"], {})


class CliTests(unittest.TestCase):
    def test_cli_writes_copy_and_never_overwrites_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "in.har"
            raw = json.dumps(har(entry("GET", API + "/a", body={}, headers=[("Authorization", "Bearer x")])))
            src.write_text(raw, encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scrub.main([str(src), "-o", str(tmp / "api" / "c.har")]), 0)
            self.assertIn("header: authorization: 1", out.getvalue())
            self.assertEqual(src.read_text(encoding="utf-8"), raw)
            written = (tmp / "api" / "c.har").read_bytes()
            self.assertNotIn(b"\r\n", written)
            self.assertIn(b"Bearer REDACTED", written)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(scrub.main([str(src), "-o", str(src)]), 2)
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(scrub.main([str(src), "-o", str(tmp / "dry.har"), "--dry-run", "--json"]), 0)
            self.assertFalse((tmp / "dry.har").exists())
            self.assertEqual(json.loads(out.getvalue())["entries_kept"], 1)

    def test_cli_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.har"
            bad.write_text("not json", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(scrub.main([str(bad), "-o", str(Path(tmp) / "o.har")]), 2)


if __name__ == "__main__":
    unittest.main()

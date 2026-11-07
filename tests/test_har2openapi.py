"""Tests for skills/apirev-infer/har2openapi.py: templates, schema merging, detection, determinism."""
import io
import json
import random
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _load import entry, har, load

h2o = load("apirev-infer", "har2openapi")
API = "https://api.shop.example"


def paths_of(templates: dict) -> dict:
    return {raw: t.path for raw, t in templates.items()}


def build(*entries, **kwargs):
    return h2o.build_spec(har(*entries), **kwargs)


def response_schema(spec, path, method="get", status="200"):
    return spec["paths"][path][method]["responses"][status]["content"]["application/json"]["schema"]


def load_yaml_subset(text: str):
    """Parse the block-YAML subset to_yaml emits (maps, sequences, JSON-quoted or plain scalars)."""
    lines = [l for l in text.split("\n") if l.strip()]
    pos = [0]
    decoder = json.JSONDecoder()

    def scalar(token: str):
        token = token.strip()
        if token.startswith('"'):
            return json.loads(token)
        if token in ("{}", "[]", "null", "true", "false") or re.match(r"^-?\d", token):
            return json.loads(token)
        return token

    def split_key(body: str):
        if body.startswith('"'):
            key, end = decoder.raw_decode(body)
            rest = body[end:]
        else:
            key, _, rest = body.partition(":")
            rest = ":" + rest
        assert rest.startswith(":"), body
        return key, rest[1:].strip()

    def is_mapping(body: str) -> bool:
        if body.startswith('"'):
            _, end = decoder.raw_decode(body)
            return body[end:].startswith(":")
        return bool(re.match(r"^[^\s:\"]+:( |$)", body))

    def block(indent: int):
        first = lines[pos[0]]
        if first[indent:].startswith("- "):
            out = []
            while pos[0] < len(lines) and lines[pos[0]].startswith(" " * indent + "- "):
                body = lines[pos[0]][indent + 2:]
                if body.startswith("- ") or is_mapping(body):
                    lines[pos[0]] = " " * (indent + 2) + body
                    out.append(block(indent + 2))
                else:
                    out.append(scalar(body))
                    pos[0] += 1
            return out
        out = {}
        while pos[0] < len(lines):
            line = lines[pos[0]]
            cur = len(line) - len(line.lstrip(" "))
            if cur < indent or line[indent:].startswith("- "):
                break
            key, rest = split_key(line[indent:])
            pos[0] += 1
            if rest:
                out[key] = scalar(rest)
            else:
                nxt = lines[pos[0]]
                child = len(nxt) - len(nxt.lstrip(" "))
                out[key] = block(child)
        return out

    return block(0)


class SegmentTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(h2o.classify_segment("42"), "integer")
        self.assertEqual(h2o.classify_segment("9b2f6c1e-5d3a-4f8e-a1c2-3e4d5f6a7b8c"), "uuid")
        self.assertEqual(h2o.classify_segment("2026-11-01"), "date")
        self.assertEqual(h2o.classify_segment("a3f9c2e1b7d4"), "hex")
        for literal in ("pets", "v1", "deadbeef", "2026-13-01", "me", "abc"):
            self.assertIsNone(h2o.classify_segment(literal), literal)

    def test_singular_and_names(self):
        self.assertEqual(h2o.singular("categories"), "category")
        self.assertEqual(h2o.singular("boxes"), "box")
        self.assertEqual(h2o.singular("status"), "status")
        self.assertEqual(h2o.camel("adoption-requests", singularize=True), "adoptionRequest")


class TemplateTests(unittest.TestCase):
    def test_numeric_ids_named_from_previous_segment(self):
        t = h2o.infer_templates(["/users/1", "/users/22", "/users/1/orders/7"])
        self.assertEqual(paths_of(t), {"/users/1": "/users/{userId}", "/users/22": "/users/{userId}",
                                       "/users/1/orders/7": "/users/{userId}/orders/{orderId}"})
        params = t["/users/1"].params
        self.assertEqual(params[0]["kind"], "integer")
        self.assertEqual(params[0]["values"], ["1", "22"])

    def test_single_sample_numeric_id_still_a_parameter(self):
        self.assertEqual(h2o.infer_templates(["/v2/invoices/90210"])["/v2/invoices/90210"].path,
                         "/v2/invoices/{invoiceId}")

    def test_uuid_date_and_hex(self):
        t = h2o.infer_templates(["/orders/9b2f6c1e-5d3a-4f8e-a1c2-3e4d5f6a7b8c",
                                 "/reports/2026-11-01", "/commits/a3f9c2e1b7d4"])
        self.assertEqual(sorted(x.path for x in t.values()),
                         ["/commits/{commitId}", "/orders/{orderId}", "/reports/{reportDate}"])
        kinds = {x.path: x.params[0]["kind"] for x in t.values()}
        self.assertEqual(kinds["/orders/{orderId}"], "uuid")
        self.assertEqual(kinds["/reports/{reportDate}"], "date")
        self.assertEqual(kinds["/commits/{commitId}"], "hex")

    def test_version_segments_stay_literal(self):
        t = h2o.infer_templates(["/v1/pets", "/v2/pets", "/v3/pets"])
        self.assertEqual(sorted(x.path for x in t.values()), ["/v1/pets", "/v2/pets", "/v3/pets"])

    def test_slugs_by_cardinality(self):
        raws = ["/shelters/north-side", "/shelters/east-end", "/shelters/lakeview-annex"]
        t = h2o.infer_templates(raws)
        self.assertEqual({x.path for x in t.values()}, {"/shelters/{shelterSlug}"})
        self.assertIn("cardinality", t[raws[0]].params[0]["reason"])

    def test_slug_children_follow_the_collapse(self):
        raws = ["/shelters/north-side", "/shelters/east-end", "/shelters/lakeview-annex",
                "/shelters/north-side/pets"]
        self.assertEqual(h2o.infer_templates(raws)["/shelters/north-side/pets"].path,
                         "/shelters/{shelterSlug}/pets")

    def test_two_slugs_are_not_enough(self):
        t = h2o.infer_templates(["/shelters/north-side", "/shelters/east-end"])
        self.assertEqual(sorted(x.path for x in t.values()), ["/shelters/east-end", "/shelters/north-side"])

    def test_min_distinct_option(self):
        raws = ["/shelters/north-side", "/shelters/east-end"]
        self.assertEqual({x.path for x in h2o.infer_templates(raws, min_distinct=2).values()},
                         {"/shelters/{shelterSlug}"})
        raws3 = raws + ["/shelters/lakeview-annex"]
        self.assertEqual(len({x.path for x in h2o.infer_templates(raws3, min_distinct=0).values()}), 3)

    def test_plain_word_collections_are_not_slugs(self):
        t = h2o.infer_templates(["/catalog/pets", "/catalog/toys", "/catalog/food", "/catalog/beds"])
        self.assertEqual(len({x.path for x in t.values()}), 4)

    def test_top_level_hyphenated_collections_are_not_slugs(self):
        raws = ["/v1/adoption-requests", "/v1/medical-records", "/v1/pet-photos"]
        self.assertEqual(sorted(x.path for x in h2o.infer_templates(raws).values()), sorted(raws))

    def test_collections_followed_by_ids_are_not_slugs(self):
        raws = ["/api/store/gift-cards/1", "/api/store/pet-food/2", "/api/store/chew-toys/3"]
        paths = sorted(x.path for x in h2o.infer_templates(raws).values())
        self.assertEqual(paths, ["/api/store/chew-toys/{chewToyId}", "/api/store/gift-cards/{giftCardId}",
                                 "/api/store/pet-food/{petFoodId}"])

    def test_literal_sibling_beats_parameter(self):
        t = h2o.infer_templates(["/pets/3", "/pets/7", "/pets/search"])
        self.assertEqual(t["/pets/search"].path, "/pets/search")
        self.assertEqual(t["/pets/3"].path, "/pets/{petId}")

    def test_duplicate_names_are_numbered(self):
        t = h2o.infer_templates(["/users/1/users/2"])
        self.assertEqual(t["/users/1/users/2"].path, "/users/{userId}/users/{userId2}")

    def test_parameter_after_parameter_gets_generic_name(self):
        self.assertEqual(h2o.infer_templates(["/pairs/1/2"])["/pairs/1/2"].path, "/pairs/{pairId}/{id}")

    def test_mixed_kinds_become_string(self):
        t = h2o.infer_templates(["/items/12", "/items/9b2f6c1e-5d3a-4f8e-a1c2-3e4d5f6a7b8c"])
        self.assertEqual(len({x.path for x in t.values()}), 1)
        self.assertEqual(t["/items/12"].params[0]["kind"], "string")
        self.assertTrue(t["/items/12"].params[0]["reason"].startswith("mixed"))

    def test_template_override(self):
        t = h2o.infer_templates(["/users/alice", "/users/bob", "/users/me/settings"],
                                overrides=["/users/{username}"])
        self.assertEqual(t["/users/alice"].path, "/users/{username}")
        self.assertEqual(t["/users/alice"].params[0]["reason"], "--template override")
        self.assertEqual(t["/users/me/settings"].path, "/users/me/settings")

    def test_root_and_trailing_slash(self):
        t = h2o.infer_templates(["/", "/pets/"])
        self.assertEqual(t["/"].path, "/")
        self.assertEqual(t["/pets/"].path, "/pets")

    def test_order_independent(self):
        raws = ["/shelters/north-side", "/pets/3", "/shelters/east-end", "/pets/7",
                "/shelters/lakeview-annex", "/pets/search"]
        expected = paths_of(h2o.infer_templates(raws))
        rng = random.Random(7)
        for _ in range(5):
            rng.shuffle(raws)
            self.assertEqual(paths_of(h2o.infer_templates(raws)), expected)


class SchemaTests(unittest.TestCase):
    def schema(self, *values):
        shape = h2o.Shape()
        for v in values:
            shape.observe(v)
        flags = []
        return h2o.to_schema(shape, "$", flags), flags

    def test_integer_and_number_merge(self):
        s, _ = self.schema(1, 2.5)
        self.assertEqual(s["type"], "number")
        s, _ = self.schema(1, 2)
        self.assertEqual(s["type"], "integer")

    def test_boolean_is_not_integer(self):
        s, _ = self.schema(True, False)
        self.assertEqual(s["type"], "boolean")

    def test_int64(self):
        s, _ = self.schema(1, 9007199254740991)
        self.assertEqual(s["format"], "int64")

    def test_null_makes_nullable(self):
        s, _ = self.schema("a", None)
        self.assertEqual((s["type"], s["nullable"]), ("string", True))

    def test_only_null_is_flagged(self):
        s, flags = self.schema(None, None)
        self.assertEqual(s, {"nullable": True})
        self.assertIn("only null", flags[0][1])

    def test_mixed_types_become_one_of(self):
        s, flags = self.schema("a", 1)
        self.assertEqual([o["type"] for o in s["oneOf"]], ["integer", "string"])
        self.assertTrue(any("mixed types" in f[1] for f in flags))

    def test_required_means_present_in_every_sample(self):
        s, _ = self.schema({"id": 1, "name": "a", "note": None}, {"id": 2, "name": "b"},
                           {"id": 3, "name": "c", "note": "x"})
        self.assertEqual(s["required"], ["id", "name"])
        self.assertEqual(s["properties"]["note"], {"type": "string", "nullable": True, "example": "x"})

    def test_required_from_few_samples_is_flagged_once(self):
        _, flags = self.schema({"id": 1, "owner": {"name": "x"}})
        self.assertEqual([f[0] for f in flags], ["$"])

    def test_rare_nested_object_is_flagged(self):
        values = [{"id": i} for i in range(4)] + [{"id": 9, "extra": {"a": 1}}]
        _, flags = self.schema(*values)
        self.assertIn("$.extra", [f[0] for f in flags])

    def test_arrays_merge_item_schemas(self):
        s, _ = self.schema([{"a": 1}, {"a": 2, "b": "x"}], [{"a": 3.5}])
        items = s["items"]
        self.assertEqual(items["properties"]["a"]["type"], "number")
        self.assertEqual(items["required"], ["a"])

    def test_empty_arrays_flagged(self):
        s, flags = self.schema([], [])
        self.assertEqual(s, {"type": "array", "items": {}})
        self.assertIn("empty arrays", flags[0][1])

    def test_formats(self):
        cases = {
            "date-time": ["2026-11-01T10:00:00Z", "2026-11-02T10:00:00.5+02:00"],
            "date": ["2026-11-01"], "email": ["a@b.example"], "uuid": ["9b2f6c1e-5d3a-4f8e-a1c2-3e4d5f6a7b8c"],
            "uri": ["https://x.example/a"], "ipv4": ["10.0.0.1"],
        }
        for fmt, values in cases.items():
            with self.subTest(fmt=fmt):
                self.assertEqual(self.schema(*values)[0]["format"], fmt)

    def test_format_needs_every_value(self):
        s, _ = self.schema("a@b.example", "not an email")
        self.assertNotIn("format", s)

    def test_enum_needs_five_samples_and_repetition(self):
        s, _ = self.schema("cat", "dog", "cat", "rabbit", "dog", "rabbit")
        self.assertEqual(s["enum"], ["cat", "dog", "rabbit"])
        s, _ = self.schema("cat", "dog", "cat", "rabbit", "dog")
        self.assertNotIn("enum", s, "3 distinct in 5 samples: values do not repeat enough")
        s, flags = self.schema("cat", "dog", "cat")
        self.assertNotIn("enum", s)
        self.assertIn("needs 5+ samples", flags[0][1])
        s, _ = self.schema("Miso", "Pepper", "Clover", "Juniper", "Rocket")
        self.assertNotIn("enum", s)
        s, _ = self.schema(*["a", "b", "c", "d", "e", "f"] * 2)
        self.assertNotIn("enum", s)

    def test_id_keyed_objects_become_maps(self):
        s, _ = self.schema({"1": {"n": 1}, "2": {"n": 2}})
        self.assertEqual(s["additionalProperties"]["properties"]["n"]["type"], "integer")
        self.assertNotIn("properties", s)

    def test_examples_come_from_first_sample(self):
        s, _ = self.schema("first", "second")
        self.assertEqual(s["example"], "first")


class SpecTests(unittest.TestCase):
    def test_groups_by_template_and_method(self):
        spec, report = build(
            entry("GET", API + "/v1/items/1", body={"id": 1}),
            entry("GET", API + "/v1/items/2", body={"id": 2}),
            entry("DELETE", API + "/v1/items/2", status=204),
            entry("GET", API + "/v1/items", body=[{"id": 1}]))
        self.assertEqual(sorted(spec["paths"]), ["/v1/items", "/v1/items/{itemId}"])
        self.assertEqual(sorted(spec["paths"]["/v1/items/{itemId}"]), ["delete", "get"])
        get = spec["paths"]["/v1/items/{itemId}"]["get"]
        self.assertEqual(get["x-apirev-samples"], 2)
        self.assertEqual(get["parameters"][0], {
            "name": "itemId", "in": "path", "required": True,
            "schema": {"type": "integer", "example": 1}, "description": "Inferred as integer id."})
        self.assertEqual(spec["paths"]["/v1/items/{itemId}"]["delete"]["responses"]["204"],
                         {"description": "No Content"})
        self.assertEqual(report["operations"], 3)

    def test_operation_ids(self):
        spec, _ = build(
            entry("GET", API + "/v1/pets", body=[]), entry("POST", API + "/v1/pets", 201, body={"id": 1}),
            entry("GET", API + "/v1/pets/1", body={}), entry("PATCH", API + "/v1/pets/1", body={}),
            entry("PUT", API + "/v1/pets/1", body={}), entry("DELETE", API + "/v1/pets/1", 204),
            entry("POST", API + "/v1/auth/login", body={}), entry("GET", API + "/v1/me", body={}))
        ids = {(p, m): op["operationId"] for p, item in spec["paths"].items() for m, op in item.items()}
        self.assertEqual(ids, {
            ("/v1/pets", "get"): "listPets", ("/v1/pets", "post"): "createPet",
            ("/v1/pets/{petId}", "get"): "getPet", ("/v1/pets/{petId}", "patch"): "updatePet",
            ("/v1/pets/{petId}", "put"): "replacePet", ("/v1/pets/{petId}", "delete"): "deletePet",
            ("/v1/auth/login", "post"): "postAuthLogin", ("/v1/me", "get"): "getMe"})

    def test_operation_id_collisions_use_parents(self):
        spec, _ = build(entry("GET", API + "/pets", body=[]), entry("GET", API + "/owners/1/pets", body=[]))
        self.assertEqual(spec["paths"]["/pets"]["get"]["operationId"], "listPets")
        self.assertEqual(spec["paths"]["/owners/{ownerId}/pets"]["get"]["operationId"], "listOwnerPets")

    def test_query_parameters(self):
        spec, _ = build(
            entry("GET", API + "/search?q=cat&page=1&tag=a&tag=b", body=[]),
            entry("GET", API + "/search?q=dog&page=2&debug=true", body=[]))
        params = {p["name"]: p for p in spec["paths"]["/search"]["get"]["parameters"]}
        self.assertTrue(params["q"]["required"])
        self.assertFalse(params["debug"]["required"])
        self.assertEqual(params["page"]["schema"]["type"], "integer")
        self.assertEqual(params["debug"]["schema"]["type"], "boolean")
        self.assertEqual(params["tag"]["schema"]["type"], "array")
        self.assertIn("Observed values: cat, dog.", params["q"]["description"])

    def test_leading_zero_query_values_stay_strings(self):
        spec, _ = build(entry("GET", API + "/zip?code=02134", body={}))
        self.assertEqual(spec["paths"]["/zip"]["get"]["parameters"][0]["schema"]["type"], "string")

    def test_request_bodies_json_and_form(self):
        spec, _ = build(
            entry("POST", API + "/things", 201, req_body={"name": "a", "size": 2}, body={"id": 1}),
            entry("POST", API + "/forms", 200, req_body="name=a&count=3",
                  req_mime="application/x-www-form-urlencoded", body={"ok": True}))
        body = spec["paths"]["/things"]["post"]["requestBody"]
        self.assertTrue(body["required"])
        self.assertEqual(body["content"]["application/json"]["schema"]["required"], ["name", "size"])
        form = spec["paths"]["/forms"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]
        self.assertEqual(form["schema"]["properties"]["count"]["type"], "integer")

    def test_responses_per_status_and_content_type(self):
        spec, _ = build(
            entry("GET", API + "/a/1", body={"id": 1}),
            entry("GET", API + "/a/2", 404, body={"error": "nope"}),
            entry("GET", API + "/a/3", 200, text="plain", mime="text/plain"))
        responses = spec["paths"]["/a/{aId}"]["get"]["responses"]
        self.assertEqual(sorted(responses), ["200", "404"])
        self.assertEqual(sorted(responses["200"]["content"]), ["application/json", "text/plain"])
        self.assertEqual(responses["404"]["description"], "Not Found")
        self.assertEqual(responses["200"]["content"]["text/plain"]["schema"], {"type": "string"})

    def test_examples_trim_arrays(self):
        spec, _ = build(entry("GET", API + "/list", body=[{"i": n} for n in range(10)]))
        example = spec["paths"]["/list"]["get"]["responses"]["200"]["content"]["application/json"]["example"]
        self.assertEqual(example, [{"i": 0}, {"i": 1}])

    def test_static_and_preflight_entries_skipped(self):
        spec, report = build(
            entry("GET", API + "/logo.png", text="x", mime="image/png", resource_type="image"),
            entry("GET", API + "/app.js", text="x", mime="application/javascript"),
            entry("GET", "https://www.shop.example/", text="<html>", mime="text/html", resource_type="document"),
            entry("OPTIONS", API + "/items", 204),
            entry("GET", API + "/items", body=[]))
        self.assertEqual(list(spec["paths"]), ["/items"])
        self.assertEqual(report["skipped"]["static asset or page"], 3)
        self.assertEqual(report["skipped"]["CORS preflight"], 1)

    def test_host_filter_and_servers(self):
        spec, report = build(entry("GET", API + "/a", body={}), entry("GET", API + "/b", body={}),
                             entry("GET", "https://other.example/c", body={}), hosts=["api.*"])
        self.assertEqual(spec["servers"], [{"url": API}])
        self.assertEqual(sorted(spec["paths"]), ["/a", "/b"])
        spec, _ = build(entry("GET", API + "/a", body={}), entry("GET", "http://localhost:8000/c", body={}),
                        entry("GET", "http://localhost:8000/d", body={}))
        self.assertEqual(spec["servers"][0], {"url": "http://localhost:8000"})

    def test_title_default(self):
        spec, _ = build(entry("GET", API + "/a", body={}))
        self.assertEqual(spec["info"]["title"], "Shop API")


class SecurityTests(unittest.TestCase):
    def schemes(self, *entries):
        spec, _ = build(*entries)
        return spec, spec.get("components", {}).get("securitySchemes", {})

    def test_bearer_jwt_after_scrub(self):
        spec, schemes = self.schemes(entry("GET", API + "/me", body={},
                                           headers=[("Authorization", "Bearer REDACTED-jwt")]))
        self.assertEqual(schemes["bearerAuth"]["bearerFormat"], "JWT")
        self.assertEqual(schemes["bearerAuth"]["scheme"], "bearer")
        self.assertEqual(spec["paths"]["/me"]["get"]["security"], [{"bearerAuth": []}])
        self.assertNotIn("REDACTED", json.dumps(schemes))

    def test_basic_api_key_header_query_cookie(self):
        _, schemes = self.schemes(
            entry("GET", API + "/a", body={}, headers=[("Authorization", "Basic REDACTED")]),
            entry("GET", API + "/b", body={}, headers=[("X-API-Key", "REDACTED")]),
            entry("GET", API + "/c?api_key=REDACTED", body={}),
            entry("GET", API + "/d", body={}, headers=[("Cookie", "session_id=REDACTED; theme=REDACTED")]))
        self.assertEqual(schemes["basicAuth"], dict(schemes["basicAuth"], type="http", scheme="basic"))
        self.assertEqual((schemes["xApiKeyHeader"]["in"], schemes["xApiKeyHeader"]["name"]), ("header", "X-API-Key"))
        self.assertEqual(schemes["apiKeyQuery"]["in"], "query")
        self.assertEqual(schemes["sessionIdCookie"]["in"], "cookie")
        self.assertNotIn("themeCookie", schemes)

    def test_auth_query_param_not_listed_as_parameter(self):
        spec, _ = self.schemes(entry("GET", API + "/c?api_key=REDACTED&q=1", body={}))
        names = [p["name"] for p in spec["paths"]["/c"]["get"]["parameters"]]
        self.assertEqual(names, ["q"])

    def test_optional_auth(self):
        spec, _ = self.schemes(entry("GET", API + "/feed", body=[], headers=[("Authorization", "Bearer REDACTED")]),
                               entry("GET", API + "/feed", body=[]))
        self.assertEqual(spec["paths"]["/feed"]["get"]["security"], [{"bearerAuth": []}, {}])

    def test_no_auth_no_security(self):
        spec, schemes = self.schemes(entry("GET", API + "/public", body={}))
        self.assertNotIn("security", spec["paths"]["/public"]["get"])
        self.assertEqual(schemes, {})


class PaginationTests(unittest.TestCase):
    def pagination(self, *entries):
        spec, report = build(*entries)
        op = next(iter(spec["paths"].values()))["get"]
        return op.get("x-apirev-pagination"), op["description"], report

    def test_page_based(self):
        p, desc, report = self.pagination(entry("GET", API + "/pets?page=1&per_page=10",
                                                body={"data": [], "next_page": 2}))
        self.assertEqual((p["style"], p["query"], p["response"]), ("page", ["page", "per_page"], ["next_page"]))
        self.assertIn("page-based", desc)
        self.assertEqual(len(report["pagination"]), 1)

    def test_offset_cursor_link(self):
        p, _, _ = self.pagination(entry("GET", API + "/a?limit=5&offset=10", body=[]))
        self.assertEqual((p["style"], p["query"]), ("offset", ["limit", "offset"]))
        p, _, _ = self.pagination(entry("GET", API + "/a?cursor=abc", body={"items": []}))
        self.assertEqual(p["style"], "cursor")
        p, _, _ = self.pagination(entry("GET", API + "/a", body={"items": [], "meta": {"next_cursor": "x"}}))
        self.assertEqual((p["style"], p["response"]), ("cursor", ["meta.next_cursor"]))
        p, _, _ = self.pagination(entry("GET", API + "/a", body=[],
                                        resp_headers=[("Link", '<https://api.shop.example/a?p=2>; rel="next"')]))
        self.assertEqual(p["style"], "link")

    def test_no_pagination(self):
        p, desc, _ = self.pagination(entry("GET", API + "/a", body={"x": 1}))
        self.assertIsNone(p)
        self.assertNotIn("Paginated", desc)


class DeterminismTests(unittest.TestCase):
    ENTRIES = [
        entry("GET", API + "/v1/pets?page=1", body={"data": [{"id": 1, "tags": ["a"]}], "next": "x"}),
        entry("GET", API + "/v1/pets/1", body={"id": 1, "name": "A", "born": "2020-01-01"}),
        entry("GET", API + "/v1/pets/2", body={"id": 2, "name": "B", "born": None}),
        entry("POST", API + "/v1/pets", 201, req_body={"name": "C"}, body={"id": 3}),
        entry("GET", API + "/v1/shops/north-end", body={"slug": "north-end"}),
        entry("GET", API + "/v1/shops/old-town", body={"slug": "old-town"}),
        entry("GET", API + "/v1/shops/river-side", body={"slug": "river-side"}),
    ]

    def test_same_input_same_bytes(self):
        a = h2o.dump_json(build(*self.ENTRIES)[0])
        b = h2o.dump_json(build(*json.loads(json.dumps(self.ENTRIES)))[0])
        self.assertEqual(a, b)
        self.assertEqual(h2o.render_report(build(*self.ENTRIES)[1]),
                         h2o.render_report(build(*self.ENTRIES)[1]))

    def test_entry_order_does_not_change_structure(self):
        def strip_examples(node):
            if isinstance(node, dict):
                return {k: strip_examples(v) for k, v in node.items() if k != "example"}
            if isinstance(node, list):
                return [strip_examples(v) for v in node]
            return node
        base = strip_examples(build(*self.ENTRIES)[0])
        shuffled = list(self.ENTRIES)
        random.Random(3).shuffle(shuffled)
        self.assertEqual(strip_examples(build(*shuffled)[0]), base)

    def test_status_phrases_do_not_depend_on_python_version(self):
        self.assertEqual(h2o._status_phrase(422), "Unprocessable Content")
        self.assertEqual(h2o._status_phrase(404), "Not Found")
        self.assertEqual(h2o._status_phrase(299), "Status 299")
        self.assertNotIn("from http import", Path(h2o.__file__).read_text(encoding="utf-8"))

    def test_json_output_is_sorted_and_ends_with_newline(self):
        text = h2o.dump_json(build(*self.ENTRIES)[0])
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        self.assertEqual(list(json.loads(text)["paths"]), sorted(json.loads(text)["paths"]))


class YamlTests(unittest.TestCase):
    def test_round_trip_spec(self):
        spec = build(*DeterminismTests.ENTRIES)[0]
        self.assertEqual(load_yaml_subset(h2o.to_yaml(spec)), json.loads(json.dumps(spec)))

    def test_tricky_scalars(self):
        data = {"200": {"a": "yes", "b": "", "c": "line\nbreak", "d": "3.0", "e": "x: y", "f": "#hash",
                        "g": "- dash", "h": "unicode é ✓", "i": None, "j": True, "k": 1.5, "l": [],
                        "m": {}, "n": [[1, 2], [{"x": 1}]], "o": "/v1/pets/{petId}", "p": "plain_word"}}
        text = h2o.to_yaml(data)
        self.assertEqual(load_yaml_subset(text), data)
        self.assertIn('"200":', text)
        self.assertIn('a: "yes"', text)
        self.assertIn("p: plain_word", text)


class UnscrubbedAndCliTests(unittest.TestCase):
    def write(self, folder: Path, data: dict) -> Path:
        path = folder / "c.har"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_detects_live_credentials(self):
        live = har(entry("GET", API + "/a", body={}, headers=[("Authorization", "Bearer abc.def")]))
        self.assertTrue(h2o.find_unscrubbed(live))
        jwt = har(entry("GET", API + "/a", body={"t": "eyJhbGciOi.eyJzdWIiOi.c2ln"}))
        self.assertIn("JWT-like token", h2o.find_unscrubbed(jwt))
        clean = har(entry("GET", API + "/a", body={}, headers=[("Authorization", "Bearer REDACTED-jwt")]))
        self.assertEqual(h2o.find_unscrubbed(clean), [])

    def test_cli_refuses_unscrubbed_and_writes_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            live = self.write(tmp, har(entry("GET", API + "/a/1", body={"id": 1},
                                             headers=[("Authorization", "Bearer s3cret-token")])))
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(h2o.main([str(live), "--out", str(tmp / "api")]), 2)
            self.assertIn("unscrubbed", err.getvalue())
            self.assertFalse((tmp / "api").exists())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(h2o.main([str(live), "--out", str(tmp / "api"), "--allow-unscrubbed",
                                           "--yaml"]), 0)
            self.assertIn("Warning", (tmp / "api" / "inference-report.md").read_text(encoding="utf-8"))
            self.assertTrue((tmp / "api" / "openapi.yaml").exists())
            raw = (tmp / "api" / "openapi.json").read_bytes()
            self.assertNotIn(b"\r\n", raw)

    def test_cli_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bad = tmp / "bad.har"
            bad.write_text("{}", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(h2o.main([str(bad)]), 2)
            static = self.write(tmp, har(entry("GET", API + "/x.png", text="x", mime="image/png")))
            with redirect_stderr(io.StringIO()):
                self.assertEqual(h2o.main([str(static), "--out", str(tmp / "o")]), 1)

    def test_report_sections(self):
        report = h2o.render_report(build(*DeterminismTests.ENTRIES, source_name="x.har")[1])
        for heading in ("## Endpoints", "## Path parameters", "## Security", "## Pagination",
                        "## Low-confidence findings", "## Rules used"):
            self.assertIn(heading, report)
        self.assertIn("`/v1/shops/{shopSlug}`", report)


if __name__ == "__main__":
    unittest.main()

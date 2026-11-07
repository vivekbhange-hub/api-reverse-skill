---
name: apirev-sdk
description: >-
  Generates zero-dependency typed clients from an OpenAPI JSON spec: TypeScript (interfaces for
  every schema and an async client class on fetch) and Python (TypedDict models and a client on
  urllib.request with an ApiError carrying status and body). Base URL and auth are passed in by
  the caller. Use when the user says "generate an SDK", "make a typed client", "TypeScript
  client for this API", "Python client for this spec", "give me types for these endpoints",
  "stop hand-writing fetch calls", or after apirev-infer or apirev-document.
argument-hint: "[path to openapi.json]"
---

# apirev-sdk: typed clients with no dependencies

Hand-written fetch calls drift from the API. This skill generates two small clients from the
spec, each a single file the user can drop into a project: no runtime packages, no code
generator to install, and types that match the samples the spec was inferred from.

## Inputs

- `api/openapi.json` (inferred, ideally documented by `apirev-document` first so method names
  read well). Any OpenAPI 3.0 JSON works, including `$ref` to `components/schemas`.
- Which languages the user wants: `ts`, `python` or `all` (default).
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `api/sdk/ts/client.ts`: interfaces, `ClientOptions`, `ApiError`, and `<Title>Client`.
- `api/sdk/python/client.py`: TypedDict models, `ApiError`, and `<Title>Client`.

## Steps

1. **Generate.**
   ```
   python3 "${CLAUDE_SKILL_DIR}/gen_sdk.py" api/openapi.json --out api/sdk --lang all
   ```
   The class name comes from `info.title` ("Pet Shelter API" -> `PetShelterAPIClient`). Method
   names come from `operationId` (camelCase in TS, snake_case in Python).

2. **Verify the Python client compiles and imports** (always do this; it takes a second):
   ```
   python3 -c "import ast,sys; ast.parse(open('api/sdk/python/client.py',encoding='utf-8').read()); print('ok')"
   ```
   For TypeScript, if the project already has `tsc`, run `npx tsc --noEmit api/sdk/ts/client.ts`
   (the client targets ES2020 + DOM lib for `fetch` and `URLSearchParams`). If not, skip it;
   the generator's output is checked structurally by this pack's tests.

3. **Show the user how to call it**, using a real operation from their spec:
   ```ts
   import { PetShelterAPIClient, ApiError } from "./api/sdk/ts/client";
   const api = new PetShelterAPIClient({
     baseUrl: "https://api.petshelter.example",
     getHeaders: async () => ({ Authorization: `Bearer ${await getToken()}` }),
   });
   const page = await api.listPets({ page: 1, limit: 20 });
   ```
   ```python
   from client import PetShelterAPIClient, ApiError
   api = PetShelterAPIClient("https://api.petshelter.example", headers={"Authorization": f"Bearer {token}"})
   pet = api.get_pet(42)
   ```
   Errors: both clients raise `ApiError` with `.status` and `.body` for any non-2xx response.

4. **Point at the weak spots.** Types are only as good as the samples. From
   `api/inference-report.md`, list fields typed `unknown` / `Any` (only null seen) and
   operations with one sample, and say where a type is probably too strict (required because
   it appeared once). Offer to run `schema-skeptic` before the SDK is shared.

5. **Wire it in** if asked: copy the file into the user's source tree (for example
   `src/api/client.ts`), add a regenerate note to their README or a script entry such as
   `"gen:api": "python3 path/to/gen_sdk.py api/openapi.json --out src/api --lang ts"`.

6. **Hand off.** `apirev-mock` serves the same routes locally so the new client can be used
   before the backend is reachable; `apirev-contract` keeps spec and backend in sync.

## How the generator maps types

| Schema | TypeScript | Python |
|---|---|---|
| string / integer, number / boolean | `string` / `number` / `boolean` | `str` / `int`, `float` / `bool` |
| enum | union of literals | `Literal[...]` |
| nullable | `T \| null` | `Optional[T]` |
| array | `Array<T>` | `List[T]` |
| object with properties | named `interface`, optional keys with `?` | `TypedDict`, optional keys via `total=False` |
| object with additionalProperties | `Record<string, T>` | `Dict[str, T]` |
| oneOf / anyOf | union | `Union[...]` |
| nothing known | `unknown` | `Any` |

Keys that are not identifiers (`content-type`, `from`) are quoted in TS and use the functional
`TypedDict(...)` form in Python. Path parameters are positional; query parameters go in an
object (TS) or keyword-only arguments (Python); request bodies are a `body` argument.
Form-encoded bodies are sent as `application/x-www-form-urlencoded`.

## What NOT to do

- Do not hand-edit the generated files; regenerate after changing the spec, or the next run
  silently drops the edits.
- Do not bake credentials, base URLs of private environments, or tokens into generated code;
  the caller injects them.
- Do not add retry, caching or auth-refresh logic to the generated client; wrap it instead.
- Do not generate or distribute an SDK for a third party's private API without permission, and
  never one designed to get around their authentication, rate limits or request signing.
- Do not claim the types are authoritative. They describe what was captured.

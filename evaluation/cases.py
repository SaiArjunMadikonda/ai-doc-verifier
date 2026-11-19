"""
Hand-labeled test cases for the documentation checkers.

Every case is a tiny project (an API definition, docs and sometimes code) where we
*know* which documentation claims are right and which are wrong, because we wrote
the mistakes in on purpose.  ``expected`` lists what a correct checker must report:

    (check, needle, verdict)   # needle: text that must appear in the finding message

Labeling rules (see ``run_eval.py`` for how they are scored):

* Every ``contradicted`` and ``uncertain`` finding the checker produces must be listed
  here.  An unlisted one counts as a false alarm (hurts precision).
* ``verified`` items are spot checks: listed ones must be found, unlisted ones are
  ignored, *except* those named in ``never_verified``: a finding that matches one of those
  and comes back verified is a false confirmation and counts as an error.
* A case with ``code`` and no ``spec`` is checked against the routes read from that code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

V, C, U = "verified", "contradicted", "uncertain"


@dataclass
class Case:
    name: str
    docs: dict[str, str]
    expected: list[tuple[str, str, str]]
    spec: dict | None = None
    code: dict[str, str] = field(default_factory=dict)
    # (check, needle) pairs that must NOT come back verified: guards against false confirmations
    never_verified: list[tuple[str, str]] = field(default_factory=list)
    run: str = "api"  # api | project


def _json_response(schema):
    return {"description": "ok", "content": {"application/json": {"schema": schema}}}


SHOP_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "shop", "version": "1.0"},
    "paths": {
        "/users": {
            "get": {
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "search", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "ok"}},
            },
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/NewUser"}}
                    }
                },
                "responses": {"201": {"description": "created"}, "422": {"description": "bad"}},
            },
        },
        "/users/{user_id}": {
            "parameters": [
                {"name": "user_id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            "get": {
                "responses": {
                    "200": _json_response({"$ref": "#/components/schemas/User"}),
                    "404": {"description": "missing"},
                }
            },
            "delete": {"responses": {"204": {"description": "gone"}}},
        },
        "/orders": {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["item_id", "qty"],
                                "properties": {
                                    "item_id": {"type": "integer"},
                                    "qty": {"type": "integer"},
                                    "note": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {"201": {"description": "created"}, "400": {"description": "bad"}},
            }
        },
        "/health": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "schemas": {
            "NewUser": {
                "type": "object",
                "required": ["email"],
                "properties": {
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                    "age": {"type": "integer"},
                },
            },
            "User": {
                "type": "object",
                "required": ["id", "email"],
                "properties": {
                    "id": {"type": "integer"},
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
        }
    },
}

API_CASES = [
    Case(
        "correct endpoints",
        spec=SHOP_SPEC,
        docs={
            "api.md": "### GET /users\n\nList users.\n\n### POST /users\n\nCreate.\n\n"
            "### DELETE /users/{user_id}\n\nRemove.\n\n### GET /health\n\nPing.\n"
        },
        expected=[
            ("endpoint", "`GET /users`", V),
            ("endpoint", "`POST /users`", V),
            ("endpoint", "`DELETE /users/{user_id}`", V),
            ("endpoint", "`GET /health`", V),
        ],
    ),
    Case(
        "typo in path",
        spec=SHOP_SPEC,
        docs={"api.md": "### GET /usres\n\nList users.\n"},
        expected=[("endpoint", "`/usres`", C)],
    ),
    Case(
        "endpoint that was never built",
        spec=SHOP_SPEC,
        docs={"api.md": "### GET /invoices\n\nList invoices.\n"},
        expected=[("endpoint", "`/invoices`", C)],
    ),
    Case(
        "wrong HTTP method",
        spec=SHOP_SPEC,
        docs={"api.md": "### PUT /users/{user_id}\n\nReplace a user.\n"},
        expected=[("endpoint", "`PUT`", C)],
    ),
    Case(
        "path placeholder named differently",
        spec=SHOP_SPEC,
        docs={"api.md": "### GET /users/{id}\n\nFetch one.\n\n### GET /users/42\n\nExample.\n"},
        expected=[("endpoint", "`GET /users/{user_id}`", V)],
    ),
    Case(
        "invented, correct and wrongly-typed parameters",
        spec=SHOP_SPEC,
        docs={
            "api.md": "### GET /users\n\n| Name | Type | Required |\n|---|---|---|\n"
            "| limit | integer | no |\n| offset | integer | no |\n| search | integer | no |\n"
        },
        expected=[
            ("parameter", "Parameter `limit`", V),
            ("parameter", "Parameter `offset`", C),
            ("parameter", "Parameter `search`", C),
        ],
    ),
    Case(
        "wrong required flag and wrong location",
        spec=SHOP_SPEC,
        docs={
            "api.md": "### POST /users\n\n| Name | In | Type | Required |\n|---|---|---|---|\n"
            "| email | body | string | no |\n| name | query | string | no |\n| age | body | integer | no |\n"
        },
        expected=[
            ("parameter", "Parameter `email`", C),
            ("parameter", "Parameter `name`", C),
            ("parameter", "Parameter `age`", V),
        ],
    ),
    Case(
        "documented response codes",
        spec=SHOP_SPEC,
        docs={
            "api.md": "### POST /orders\n\nResponses:\n\n- `201` created\n- `404` order not found\n"
        },
        expected=[("response", "Response `201`", V), ("response", "Response `404`", C)],
    ),
    Case(
        "deployment prefix cannot be confirmed",
        spec=SHOP_SPEC,
        docs={"api.md": "### GET /api/v2/health\n\nLiveness.\n"},
        expected=[("endpoint", "may include a deployment prefix", U)],
    ),
    Case(
        "curl examples",
        spec=SHOP_SPEC,
        docs={
            "api.md": "```bash\ncurl http://localhost:8000/users?limit=5\n"
            "curl http://localhost:8000/users?page=2\n"
            "curl -X POST http://localhost:8000/orders -H 'Content-Type: application/json' "
            "-d '{\"item_id\": 1}'\n"
            "curl https://api.github.com/users/octocat\n```\n"
        },
        expected=[
            ("example", "query parameter `page`", C),
            ("example", "omits the required body field `qty`", C),
            ("example", "`GET /users` matches", V),
        ],
    ),
    Case(
        "sample responses",
        spec=SHOP_SPEC,
        docs={
            "api.md": "### GET /users/{user_id}\n\nExample response:\n\n```json\n"
            '{"id": "7", "email": "a@b.c", "nickname": "al"}\n```\n'
        },
        expected=[
            ("example-response", "`$.id` is string", C),
            ("example-response", "`$.nickname` is in the example", C),
        ],
    ),
    Case(
        "correct sample response",
        spec=SHOP_SPEC,
        docs={
            "api.md": "### GET /users/{user_id}\n\nExample response:\n\n```json\n"
            '{"id": 7, "email": "a@b.c", "name": "Al"}\n```\n'
        },
        expected=[("example-response", "matches the schema", V)],
    ),
]

PROJECT_FILES = {
    "pyproject.toml": '[project]\nname = "shop"\nrequires-python = ">=3.11"\n'
    '[project.scripts]\nshopctl = "shop.cli:main"\n',
    "shop/__init__.py": "",
    "shop/settings.py": 'import os\n\nKEY = os.environ["SHOP_API_KEY"]\nDB = os.getenv("SHOP_DB_URL")\n',
    "shop/cli.py": (
        "import argparse\n\n\ndef build_order(items):\n    return items\n\n\n"
        "def main():\n    p = argparse.ArgumentParser(prog='shopctl')\n"
        "    p.add_argument('--dry-run', action='store_true')\n"
        "    p.add_argument('--output-format')\n    return p\n"
    ),
}

PROJECT_CASES = [
    Case(
        "environment variables",
        code=PROJECT_FILES,
        run="project",
        docs={
            "setup.md": "Set the environment variable `SHOP_API_KEY` and `SHOP_DB_URL`.\n"
            "The env var `SHOP_API_TOKEN` is also needed, and `SHOP_NEVER_USED` too.\n"
            "```bash\nexport SHOP_API_KEY=abc\nexport PATH=$PATH:/opt/bin\n```\n"
        },
        expected=[
            ("env", "`SHOP_API_KEY` is read", V),
            ("env", "`SHOP_DB_URL` is read", V),
            ("env", "`SHOP_API_TOKEN` is documented but the code reads `SHOP_API_KEY`", C),
            ("env", "`SHOP_NEVER_USED` is documented but never read", C),
        ],
    ),
    Case(
        "CLI flags",
        code=PROJECT_FILES,
        run="project",
        docs={
            "cli.md": "Use `--dry-run` or `--output-format`. The `--verbose` option is handy.\n"
            "```bash\nshopctl --dry-run\npip install shop --upgrade\n```\n"
        },
        expected=[
            ("flag", "`--dry-run` exists", V),
            ("flag", "`--output-format` exists", V),
            ("flag", "`--verbose` is documented but not defined", C),
        ],
    ),
    Case(
        "file and function references",
        code=PROJECT_FILES,
        run="project",
        docs={
            "arch.md": "Settings live in `shop/settings.py`; the old `shop/config.py` is gone.\n"
            "Call `build_order()` (not `build_orders()`). Uses `os.path.join()` and Node.js.\n"
        },
        expected=[
            ("reference", "file `shop/settings.py` exists", V),
            ("reference", "`shop/config.py` does not exist", C),
            ("reference", "`build_order` is defined", V),
            ("reference", "`build_orders()` is documented but the code defines `build_order`", C),
        ],
    ),
    Case(
        "version claims",
        code=PROJECT_FILES,
        run="project",
        docs={
            "a.md": "Requires Python 3.8+.\n",
            "b.md": "Requires Python 3.11 or newer.\n",
            "c.md": "CI also runs Python 3.9 for linting.\n",
        },
        expected=[
            ("version", "Python 3.8+ but the project requires Python 3.11+", C),
            ("version", "Python 3.11+ matches", V),
        ],
    ),
    Case(
        "unrelated technical text produces no alarms",
        code=PROJECT_FILES,
        run="project",
        docs={
            "notes.md": "Install with `pip install --upgrade pip`, then `docker run --rm alpine`.\n"
            "Visit example.com/docs.md. Uses `print()` and `len()` and `requests.get()`.\n"
            "Node.js 18 is not needed. See https://github.com/x/y/blob/main/a.py.\n"
        },
        expected=[],
    ),
]

UNRESOLVED_CASES = [
    Case(
        "route path declared through a constant",
        code={
            "main.py": 'from fastapi import FastAPI\napp = FastAPI()\nUSERS = "/users"\n\n'
            "@app.get(USERS)\ndef users(): ...\n\n@app.get('/health')\ndef health(): ...\n"
        },
        docs={"api.md": "### GET /users\n\nList.\n\n### GET /missing\n\nGone.\n"},
        expected=[("endpoint", "`GET /users` exists", V), ("endpoint", "`/missing`", C)],
    ),
    Case(
        "route path that cannot be known statically",
        code={
            "main.py": "import os\nfrom fastapi import FastAPI\napp = FastAPI()\n"
            "P = os.environ['P']\n\n@app.get(P + '/x')\ndef x(): ...\n\n"
            "@app.get('/health')\ndef health(): ...\n"
        },
        docs={"api.md": "### GET /health\n\nok\n\n### GET /secret\n\nmaybe\n"},
        expected=[
            ("endpoint", "`GET /health` exists", V),
            ("endpoint", "`/secret` is not among the routes", U),
        ],
        never_verified=[("endpoint", "`/secret`")],
    ),
    Case(
        "parameter type behind an unresolved external reference",
        spec={
            "openapi": "3.0.0",
            "info": {"title": "x", "version": "1"},
            "paths": {
                "/items": {
                    "get": {
                        "parameters": [
                            {"name": "q", "in": "query", "schema": {"$ref": "other.yaml#/Q"}},
                            {"name": "n", "in": "query", "schema": {"type": "integer"}},
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        },
        docs={
            "api.md": "### GET /items\n\n| Name | Type |\n|---|---|\n"
            "| q | integer |\n| n | integer |\n"
        },
        expected=[
            ("parameter", "Parameter `q` exists, but its type", U),
            ("parameter", "Parameter `n`", V),
        ],
        never_verified=[("parameter", "Parameter `q`")],
    ),
]

ALL_CASES = API_CASES + UNRESOLVED_CASES + PROJECT_CASES

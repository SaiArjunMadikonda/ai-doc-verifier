"""Tests for reading routes straight from source code (src/static_routes.py)."""

import textwrap

import pytest

import api_contract as ac
import static_routes as sr
from api_contract import CONTRADICTED, UNCERTAIN, VERIFIED


def write(root, files):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content))
    return root


FASTAPI_PROJECT = {
    "app/__init__.py": "",
    "app/models.py": """\
        from typing import Optional
        from pydantic import BaseModel, Field

        class Base(BaseModel):
            id: int = 0

        class UserIn(Base):
            email: str
            age: Optional[int] = None
            nickname: str = Field("anon", max_length=10)

        class UserOut(BaseModel):
            id: int
            email: str

        class Node(BaseModel):
            name: str
            parent: "Node | None" = None
        """,
    "app/routers/__init__.py": "",
    "app/routers/users.py": """\
        from fastapi import APIRouter, Depends, HTTPException, Query, Header, status
        from ..models import UserIn, UserOut

        router = APIRouter(prefix="/users", tags=["users"])

        def current_user(): ...

        @router.get("/", response_model=list[UserOut])
        async def list_users(limit: int = 10, q: str = Query(...), x_token: str = Header(None)):
            return []

        @router.get("/{user_id}", response_model=UserOut, responses={403: {"description": "no"}})
        def get_user(user_id: int):
            if user_id < 0:
                raise HTTPException(status_code=404)
            raise HTTPException(status.HTTP_410_GONE)

        @router.post("/", status_code=201)
        def create_user(payload: UserIn):
            return payload

        @router.delete("/{user_id}")
        def delete_user(user_id: int, user=Depends(current_user)):
            return None
        """,
    "app/main.py": """\
        from fastapi import FastAPI
        from .routers import users as users_module
        from .routers.users import router as users_router

        app = FastAPI()
        api = FastAPI()
        api.include_router(users_router, prefix="/v1")
        app.include_router(users_module.router)

        @app.get("/health")
        def health():
            return {"ok": True}

        @app.api_route("/echo", methods=["GET", "POST"])
        def echo():
            ...
        """,
}


@pytest.fixture
def fastapi_root(tmp_path):
    return write(tmp_path, FASTAPI_PROJECT)


def spec_of(root):
    return ac.load_spec_from_code(root)


def test_detects_frameworks(fastapi_root):
    assert sr.detect_frameworks(fastapi_root) == ["fastapi"]


def test_fastapi_paths_methods_and_prefixes(fastapi_root):
    spec = spec_of(fastapi_root)
    ops = {(m, spec.operations[(m, p)].path) for (m, p) in spec.operations}
    assert ("GET", "/health") in ops
    assert ("GET", "/users") in ops or ("GET", "/users/") in ops
    assert ("GET", "/users/{user_id}") in ops
    assert ("POST", "/echo") in ops and ("GET", "/echo") in ops
    # mounted twice (once through `users_module.router`, once with the /v1 prefix)
    assert any(path.startswith("/v1/users") for _, path in ops)


def test_fastapi_parameters(fastapi_root):
    spec = spec_of(fastapi_root)
    op = spec.operations[("GET", "/users")]
    params = {p.name: p for p in op.params}
    assert params["limit"].location == "query" and not params["limit"].required
    assert params["limit"].types == frozenset({"integer"})
    assert params["q"].required  # Query(...)
    assert params["x-token"].location == "header"  # underscores become hyphens
    assert not op.unresolved


def test_fastapi_body_models_and_inheritance(fastapi_root):
    spec = spec_of(fastapi_root)
    body = {p.name: p for p in spec.operations[("POST", "/users")].params if p.location == "body"}
    assert set(body) == {"id", "email", "age", "nickname"}  # inherited field included
    assert body["email"].required and not body["age"].required and not body["nickname"].required


def test_fastapi_responses(fastapi_root):
    spec = spec_of(fastapi_root)
    assert spec.operations[("GET", "/users/{}")].responses >= {"200", "403", "404", "410", "422"}
    assert "201" in spec.operations[("POST", "/users")].responses


def test_fastapi_depends_makes_operation_unresolved(fastapi_root):
    assert spec_of(fastapi_root).operations[("DELETE", "/users/{}")].unresolved


def test_self_referencing_model_does_not_hang(tmp_path):
    write(
        tmp_path,
        {
            "m.py": """\
            from fastapi import FastAPI
            from pydantic import BaseModel
            app = FastAPI()
            class Node(BaseModel):
                name: str
                parent: "Node | None" = None
                kids: list["Node"] = []
            @app.post("/nodes")
            def make(n: Node): ...
            """
        },
    )
    body = [p.name for p in spec_of(tmp_path).operations[("POST", "/nodes")].params]
    assert body == ["name", "parent", "kids"]


def test_fastapi_end_to_end_with_docs(fastapi_root):
    spec = spec_of(fastapi_root)
    claims = ac.extract_claims(
        textwrap.dedent(
            """\
            ### GET /users

            | Name | Type | Required |
            |---|---|---|
            | limit | string | no |
            | q | string | yes |
            | sort | string | no |

            ### POST /users

            - `email` (string, required)
            - `bogus` (string)

            ### GET /nothing
            """
        )
    )
    report = ac.verify(claims, spec)
    by_msg = {f.message: f.verdict for f in report.findings}
    assert any("Parameter `limit`" in m and v == CONTRADICTED for m, v in by_msg.items())
    assert any("Parameter `q` (query) matches" in m and v == VERIFIED for m, v in by_msg.items())
    assert any("`sort` is documented but not in the spec" in m for m in by_msg)
    assert any("`bogus` is documented but not in the spec" in m for m in by_msg)
    assert any("does not define the path `/nothing`" in m for m in by_msg)


def test_unresolved_operation_gives_uncertain_not_contradicted(fastapi_root):
    spec = spec_of(fastapi_root)
    report = ac.verify(
        ac.extract_claims("### DELETE /users/{user_id}\n\n- `force` (boolean, optional)\n"), spec
    )
    assert [f.verdict for f in report.findings if "force" in f.message] == [UNCERTAIN]


# ------------------------------------------------------------------------ Flask


FLASK_PROJECT = {
    "app.py": """\
        from flask import Flask, request
        from api import bp

        app = Flask(__name__)
        app.register_blueprint(bp, url_prefix="/api")

        @app.route("/")
        def index():
            return "hi"
        """,
    "api.py": """\
        from flask import Blueprint, request, jsonify

        bp = Blueprint("api", __name__)

        @bp.route("/items/<int:item_id>", methods=["GET", "PUT"])
        def item(item_id):
            return jsonify(request.args.get("verbose"))

        @bp.post("/items")
        def create():
            return request.args["dry_run"]
        """,
}


def test_flask_blueprints_methods_and_args(tmp_path):
    spec = spec_of(write(tmp_path, FLASK_PROJECT))
    keys = set(spec.operations)
    assert {
        ("GET", "/"),
        ("GET", "/api/items/{}"),
        ("PUT", "/api/items/{}"),
        ("POST", "/api/items"),
    } <= keys
    get = spec.operations[("GET", "/api/items/{}")]
    names = {(p.name, p.location, next(iter(p.types)) if p.types else None) for p in get.params}
    assert ("item_id", "path", "integer") in names and ("verbose", "query", None) in names
    assert get.unresolved  # Flask reads inputs dynamically


# ------------------------------------------------------------------------ Django


DJANGO_PROJECT = {
    "proj/urls.py": """\
        from django.urls import path, include
        urlpatterns = [path("api/", include("shop.urls")), path("health/", health)]
        """,
    "shop/__init__.py": "",
    "shop/urls.py": """\
        from django.urls import path, re_path
        from rest_framework.routers import DefaultRouter
        from . import views

        router = DefaultRouter()
        router.register(r"carts", views.CartViewSet)

        urlpatterns = [
            path("products/<int:pk>/", views.product, name="product"),
            path("orders/", views.OrderView.as_view()),
            re_path(r"^legacy/(?P<year>\\d+)/$", views.legacy),
            path("mystery/", views.unknown_thing),
            path("", include(router.urls)),
        ]
        """,
    "shop/views.py": """\
        from rest_framework.decorators import api_view
        from django.views.decorators.http import require_GET
        from django.views import View

        @api_view(["GET", "POST"])
        def product(request, pk): ...

        @require_GET
        def legacy(request, year): ...

        class OrderView(View):
            def get(self, request): ...
            def post(self, request): ...

        def health(request): ...
        """,
}


def test_django_urls_includes_views_and_routers(tmp_path):
    spec = spec_of(write(tmp_path, DJANGO_PROJECT))
    keys = set(spec.operations)
    assert ("GET", "/api/products/{}") in keys and ("POST", "/api/products/{}") in keys
    assert {("GET", "/api/orders"), ("POST", "/api/orders")} <= keys
    assert ("GET", "/api/legacy/{}") in keys
    assert "/api/mystery" in spec.unknown_method_paths
    assert (
        "/api/carts" in spec.unknown_method_paths and "/api/carts/{}" in spec.unknown_method_paths
    )
    assert spec.operations[("GET", "/api/products/{}")].params[0].types == frozenset({"integer"})


def test_django_unknown_methods_are_uncertain(tmp_path):
    spec = spec_of(write(tmp_path, DJANGO_PROJECT))
    report = ac.verify(ac.extract_claims("GET /api/mystery/\n\nPATCH /api/carts/\n"), spec)
    assert [f.verdict for f in report.findings] == [UNCERTAIN, UNCERTAIN]
    assert not report.undocumented_operations or "mystery" not in " ".join(
        report.undocumented_operations
    )


# ------------------------------------------------------------------------ Express


EXPRESS_PROJECT = {
    "package.json": "{}",
    "server.js": """\
        const express = require('express');
        const usersRouter = require('./routes/users');
        const app = express();
        app.use('/api/users', usersRouter);
        app.get('/ping', (req, res) => res.send('pong'));
        """,
    "routes/users.js": """\
        const express = require('express');
        const router = express.Router();
        router.get('/', (req, res) => { const n = req.query.limit; res.json([]); });
        router.post('/:id/notes', (req, res) => { const t = req.body.text; res.json({}); });
        module.exports = router;
        """,
}


def test_express_routes_mounts_and_params(tmp_path):
    root = write(tmp_path, EXPRESS_PROJECT)
    assert sr.detect_frameworks(root) == ["express"]
    spec = spec_of(root)
    assert {("GET", "/ping"), ("GET", "/api/users"), ("POST", "/api/users/{}/notes")} <= set(
        spec.operations
    )
    listing = spec.operations[("GET", "/api/users")]
    assert [(p.name, p.location) for p in listing.params] == [("limit", "query")]
    post = spec.operations[("POST", "/api/users/{}/notes")]
    assert {(p.name, p.location) for p in post.params} == {("id", "path"), ("text", "body")}


# ------------------------------------------------------------------------ misc


def test_no_framework_raises(tmp_path):
    write(tmp_path, {"x.py": "print('hi')\n"})
    with pytest.raises(ac.SpecError):
        spec_of(tmp_path)


def test_framework_without_routes_raises(tmp_path):
    write(tmp_path, {"x.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    with pytest.raises(ac.SpecError, match="could not read any routes"):
        spec_of(tmp_path)


def test_syntax_errors_are_skipped(tmp_path):
    write(
        tmp_path,
        {
            "broken.py": "def (:\n",
            "ok.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/a')\ndef a(): ...\n",
        },
    )
    assert ("GET", "/a") in spec_of(tmp_path).operations


def test_tests_and_venv_dirs_are_ignored(tmp_path):
    write(
        tmp_path,
        {
            "ok.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/real')\ndef a(): ...\n",
            "tests/t.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/fake')\ndef a(): ...\n",
            ".venv/x.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/venv')\ndef a(): ...\n",
        },
    )
    assert set(spec_of(tmp_path).operations) == {("GET", "/real")}


def test_path_converters_are_normalised(tmp_path):
    write(
        tmp_path,
        {
            "m.py": "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/files/{name:path}')\ndef f(name: str): ...\n"
        },
    )
    assert spec_of(tmp_path).operations[("GET", "/files/{}")].path == "/files/{name}"


def test_join_paths():
    assert sr.join_paths("/a/", "/b") == "/a/b"
    assert sr.join_paths("", "") == "/"
    assert sr.join_paths("", "/x") == "/x"


def test_cli_code_flag_and_autodetect(fastapi_root, tmp_path, capsys, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_text("### GET /health\n\n### GET /nope\n")
    assert ac.main(["--code", str(fastapi_root), str(docs)]) == 1
    out = capsys.readouterr().out
    assert "routes in " in out and "/nope" in out
    monkeypatch.chdir(fastapi_root)  # no spec file there -> falls back to reading the code
    assert ac.main([str(docs)]) == 1


def test_resolve_spec_prefers_explicit_spec_file(fastapi_root, tmp_path):
    import json

    from tests.api_fixtures import SPEC

    spec_file = tmp_path / "openapi.json"
    spec_file.write_text(json.dumps(SPEC))
    assert ac.resolve_spec(spec=spec_file, code=fastapi_root).source == "openapi.json"


def test_dependency_aliases_mark_operation_unresolved(tmp_path):
    write(
        tmp_path,
        {
            "deps.py": """\
            from typing import Annotated
            from fastapi import Depends
            SessionDep = Annotated[object, Depends(lambda: 1)]
            """,
            "main.py": """\
            from fastapi import FastAPI
            from deps import SessionDep
            app = FastAPI()
            @app.get("/a")
            def a(session: SessionDep, limit: int = 1): ...
            @app.get("/b")
            def b(limit: int = 1): ...
            """,
        },
    )
    spec = spec_of(tmp_path)
    assert spec.operations[("GET", "/a")].unresolved
    assert not spec.operations[("GET", "/b")].unresolved
    assert [p.name for p in spec.operations[("GET", "/a")].params] == ["limit"]


def test_nested_routers_across_three_files(tmp_path):
    write(
        tmp_path,
        {
            "api/__init__.py": "",
            "api/v1/__init__.py": "",
            "api/v1/items.py": """\
            from fastapi import APIRouter
            router = APIRouter()
            @router.get("/")
            def items(): ...
            """,
            "api/main.py": """\
            from fastapi import APIRouter
            from api.v1 import items
            api_router = APIRouter()
            api_router.include_router(items.router, prefix="/items")
            """,
            "main.py": """\
            from fastapi import FastAPI
            from api.main import api_router
            app = FastAPI()
            app.include_router(api_router, prefix="/api/v1")
            """,
        },
    )
    assert ("GET", "/api/v1/items") in spec_of(tmp_path).operations

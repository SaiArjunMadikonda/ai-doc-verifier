"""Static route readers for Go, Spring, Rails and ASP.NET (nothing is executed)."""

import pytest

from api_contract import load_spec_from_code, resolve_spec, verify_docs
from static_routes import detect_frameworks, extract_routes

GO = 'package main\n\nimport (\n\t"net/http"\n\t"github.com/gin-gonic/gin"\n\t"github.com/go-chi/chi/v5"\n)\n\nfunc main() {\n\tr := gin.Default()\n\tapi := r.Group("/api")\n\tv1 := api.Group("/v1")\n\tv1.GET("/users/:id", getUser)\n\tv1.POST("/users", createUser)\n\tr.DELETE("/health", h)\n\tresp, _ := http.Get("/not-a-route")\n\t_ = resp\n\n\tcr := chi.NewRouter()\n\tcr.Route("/orders", func(cr chi.Router) {\n\t\tcr.Get("/", list)\n\t\tcr.Route("/{id}", func(cr chi.Router) {\n\t\t\tcr.Put("/", update)\n\t\t})\n\t})\n\thttp.HandleFunc("GET /ping", ping)\n\thttp.HandleFunc("/legacy", legacy)\n}\n'
SPRING = 'package shop;\n\nimport org.springframework.web.bind.annotation.*;\n\n@RestController\n@RequestMapping("/api/users")\npublic class UserController {\n\n    @GetMapping\n    public List<User> list(@RequestParam(defaultValue = "20") int limit, @RequestParam String search) { return null; }\n\n    @GetMapping("/{id}")\n    public User get(@PathVariable Long id) { return null; }\n\n    @PostMapping(value = "/bulk", produces = "application/json")\n    public void bulk() {}\n\n    @RequestMapping(value = "/any")\n    public void any() {}\n\n    @RequestMapping(value = "/both", method = {RequestMethod.GET, RequestMethod.POST})\n    public void both() {}\n}\n'
RAILS = "Rails.application.routes.draw do\n  root to: \"home#index\"\n  namespace :api do\n    resources :users, only: [:index, :show] do\n      member do\n        post :activate\n      end\n      collection do\n        get :search\n      end\n      resources :posts\n    end\n    resource :profile, only: [:show, :update]\n  end\n  get '/health', to: 'health#show'\nend\n"
ASPNET = 'using Microsoft.AspNetCore.Mvc;\n\n[ApiController]\n[Route("api/[controller]")]\npublic class OrdersController : ControllerBase\n{\n    [HttpGet]\n    public IActionResult List() => Ok();\n\n    [HttpGet("{id:int}")]\n    public IActionResult Get(int id) => Ok();\n\n    [HttpPost("[action]")]\n    public IActionResult Import() => Ok();\n}\n'
ASPNET_MIN = 'var app = WebApplication.CreateBuilder(args).Build();\nvar g = app.MapGroup("/v2");\ng.MapGet("/ping", () => "pong");\napp.MapDelete("/items/{id}", (int id) => 1);\n'


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def routes(spec):
    return sorted(f"{m.upper()} {p}" for p, item in spec["paths"].items() for m in item)


def test_go_gin_chi_and_net_http(tmp_path):
    write(tmp_path, "main.go", GO)
    assert detect_frameworks(tmp_path) == ["go"]
    spec = extract_routes(tmp_path)
    assert routes(spec) == [
        "DELETE /health",
        "GET /api/v1/users/{id}",
        "GET /orders",
        "GET /ping",
        "POST /api/v1/users",
        "PUT /orders/{id}",
    ]  # http.Get("/not-a-route") is a client call, not a route
    assert spec["x-unknown-methods"] == ["/legacy"]  # HandleFunc without a method


def test_go_test_files_are_ignored(tmp_path):
    write(tmp_path, "a_test.go", GO)
    assert detect_frameworks(tmp_path) == []


def test_spring_controller(tmp_path):
    write(tmp_path, "src/UserController.java", SPRING)
    spec = extract_routes(tmp_path)
    assert routes(spec) == [
        "GET /api/users",
        "GET /api/users/both",
        "GET /api/users/{id}",
        "POST /api/users/both",
        "POST /api/users/bulk",
    ]
    assert spec["x-unknown-methods"] == ["/api/users/any"]
    params = {p["name"]: p["required"] for p in spec["paths"]["/api/users"]["get"]["parameters"]}
    assert params == {"limit": False, "search": True}


def test_rails_routes(tmp_path):
    write(tmp_path, "config/routes.rb", RAILS)
    spec = extract_routes(tmp_path)
    got = routes(spec)
    for expected in (
        "GET /",
        "GET /health",
        "GET /api/users",
        "GET /api/users/{id}",
        "POST /api/users/{id}/activate",
        "GET /api/users/search",
        "GET /api/users/{user_id}/posts",
        "DELETE /api/users/{user_id}/posts/{id}",
        "GET /api/profile",
        "PATCH /api/profile",
        "PUT /api/profile",
    ):
        assert expected in got, expected
    assert "DELETE /api/users/{id}" not in got  # only: [:index, :show]
    assert "POST /api/users" not in got
    assert "GET /api/profile/{id}" not in got  # a singular resource has no id


def test_aspnet_controllers_and_minimal_apis(tmp_path):
    write(tmp_path, "OrdersController.cs", ASPNET)
    write(tmp_path, "Program.cs", ASPNET_MIN)
    spec = extract_routes(tmp_path)
    assert routes(spec) == [
        "DELETE /items/{id}",
        "GET /api/orders",
        "GET /api/orders/{id}",
        "GET /v2/ping",
        "POST /api/orders/import",
    ]


def test_docs_are_checked_against_these_routes(tmp_path):
    write(tmp_path, "main.go", GO)
    write(
        tmp_path, "docs/api.md", "### GET /api/v1/users/{id}\n\nOne user.\n\n### GET /api/v1/gone\n"
    )
    spec = load_spec_from_code(tmp_path)
    report = verify_docs(spec, [tmp_path / "docs"], tmp_path)
    by = {(f.claim.raw_path, f.verdict) for f in report.findings if f.check == "endpoint"}
    assert ("/api/v1/users/{id}", "verified") in by
    assert ("/api/v1/gone", "contradicted") in by


def test_documented_parameters_are_uncertain_not_wrong(tmp_path):
    write(tmp_path, "UserController.java", SPRING)
    write(
        tmp_path,
        "docs/api.md",
        "### GET /api/users\n\n| Name | Type | Required |\n|---|---|---|\n| tenant | string | no |\n",
    )
    report = verify_docs(load_spec_from_code(tmp_path), [tmp_path / "docs"], tmp_path)
    params = [f for f in report.findings if f.check == "parameter"]
    assert params and all(f.verdict != "contradicted" for f in params)


def test_nothing_found_message_lists_new_frameworks(tmp_path):
    write(tmp_path, "readme.txt", "nothing")
    with pytest.raises(Exception) as err:
        resolve_spec(None, str(tmp_path), None, tmp_path)
    assert "ASP.NET" in str(err.value)

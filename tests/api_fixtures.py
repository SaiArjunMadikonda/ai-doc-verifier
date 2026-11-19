"""Shared OpenAPI sample used by the verifier tests."""

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/users": {
            "get": {
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "ok"}, "422": {"description": "v"}},
            },
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/UserIn"}}
                    }
                },
                "responses": {"201": {"description": "c"}},
            },
        },
        "/users/{user_id}": {
            "parameters": [
                {"name": "user_id", "in": "path", "required": True, "schema": {"type": "integer"}}
            ],
            "get": {"responses": {"200": {"description": "ok"}, "404": {"description": "nf"}}},
        },
        "/health": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/search": {"get": {"responses": {"200": {"description": "ok"}, "default": {}}}},
        "/ext": {
            "get": {
                "parameters": [{"$ref": "other.yaml#/components/parameters/Shared"}],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
    "components": {
        "schemas": {
            "Base": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "UserIn": {
                "allOf": [
                    {"$ref": "#/components/schemas/Base"},
                    {
                        "type": "object",
                        "required": ["email"],
                        "properties": {
                            "email": {"type": "string"},
                            "age": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                        },
                    },
                ]
            },
        }
    },
}

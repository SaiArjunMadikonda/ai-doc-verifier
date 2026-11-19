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


EXAMPLE_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "shop", "version": "1"},
    "servers": [{"url": "https://api.shopco.io/v1"}],
    "paths": {
        "/users": {
            "get": {
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/User"},
                                }
                            }
                        },
                    }
                },
            },
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/UserIn"}}
                    }
                },
                "responses": {
                    "201": {
                        "description": "created",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                        },
                    },
                    "404": {"description": "nope"},
                },
            },
        },
        "/users/{user_id}": {
            "get": {
                "parameters": [
                    {
                        "name": "user_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/User"}}
                        },
                    }
                },
            }
        },
    },
    "components": {
        "schemas": {
            "UserIn": {
                "type": "object",
                "required": ["email"],
                "properties": {"email": {"type": "string"}, "age": {"type": "integer"}},
            },
            "User": {
                "type": "object",
                "required": ["id", "email"],
                "properties": {
                    "id": {"type": "integer"},
                    "email": {"type": "string"},
                    "age": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "address": {"$ref": "#/components/schemas/Address"},
                },
            },
            "Address": {
                "type": "object",
                "required": ["city"],
                "properties": {"city": {"type": "string"}, "zip": {"type": "string"}},
            },
        }
    },
}

# Shop API

## Users

### GET /users

Returns a list of users.

| Name | Type | Required | Description |
|---|---|---|---|
| limit | integer | no | Maximum number of users |
| offset | integer | no | Number of users to skip |
| search | string | no | Filter by name |

### POST /users

Creates a user. Returns `201` on success and `409` if the email already exists.

| Name | In | Type | Required |
|---|---|---|---|
| email | body | string | no |
| name | body | string | no |
| age | body | string | no |

### GET /users/{id}

Fetch one user.

```bash
curl http://localhost:8000/users/42
curl -X POST http://localhost:8000/users -d '{"email": "a@b.c", "nickname": "al"}'
```

### PUT /users/{user_id}

Replace a user.

## Orders

### POST /orders

Place an order. Responses: `201` created, `400` invalid order.

```bash
curl -X POST http://localhost:8000/orders -H "Content-Type: application/json" -d '{"item_id": 1}'
```

## Billing

### GET /invoices

List invoices.

## Health

### GET /health

Liveness probe.

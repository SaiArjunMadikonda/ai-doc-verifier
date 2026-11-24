# 📚 Documentation audit

| Verdict | Checks |
|---|---|
| ✅ Verified | 24 |
| ❌ Contradicted | 19 |
| ❓ Uncertain | 1 |

**Documentation health: 55.7 / 100**

Needs the most attention:
- `docs/operations.md`: 50/100 (4 contradicted, 0 uncertain)
- `docs/setup.md`: 50/100 (6 contradicted, 1 uncertain)
- `docs/api.md`: 60.9/100 (9 contradicted, 0 uncertain)

## 🔎 API Contract Verification

Checked **7** documented endpoint(s) and **3** example(s) against `routes in shop-demo`.

| Verdict | Checks |
|---|---|
| ✅ Verified | 11 |
| ❌ Contradicted | 9 |
| ❓ Uncertain | 0 |

**Documentation health: 55 / 100**

Needs the most attention:
- `docs/api.md`: 55/100 (9 contradicted, 0 uncertain)

### ❌ Contradicted by the spec (9)

- **GET /users** — Parameter `offset` is documented but not in the spec for `GET /users`. Spec parameters: `limit`, `search`.
  `docs/api.md:5` · evidence: `routes in shop-demo#/paths/~1users/get`
  💡 Suggested fix: Remove `offset` from the docs, or add it to the API if it should exist.
- **POST /users** — Parameter `email`: docs say optional, spec says required.
  `docs/api.md:15` · evidence: `routes in shop-demo#/paths/~1users/post/requestBody/properties/email`
  💡 Suggested fix: Mark `email` as required.
- **POST /users** — Parameter `age`: docs say type `string`, spec says `integer`.
  `docs/api.md:15` · evidence: `routes in shop-demo#/paths/~1users/post/requestBody/properties/age`
  💡 Suggested fix: Change the type of `age` to `integer`.
- **POST /users** — Response `409` is documented but the spec only defines `201`, `422`.
  `docs/api.md:15` · evidence: `routes in shop-demo#/paths/~1users/post/responses`
  💡 Suggested fix: Remove `409` from the docs, or add that response to the API.
- **PUT /users/{user_id}** — Docs say `PUT` but the spec defines DELETE, GET for `/users/{user_id}`.
  `docs/api.md:34` · evidence: `routes in shop-demo#/paths/~1users~1{user_id}`
  💡 Suggested fix: Use one of DELETE, GET for `/users/{user_id}` in the docs.
- **POST /orders** — Response `400` is documented but the spec only defines `201`, `422`.
  `docs/api.md:40` · evidence: `routes in shop-demo#/paths/~1orders/post/responses`
  💡 Suggested fix: Remove `400` from the docs, or add that response to the API.
- **GET /invoices** — The spec does not define the path `/invoices`.
  `docs/api.md:50` · evidence: `routes in shop-demo (searched 4 paths)`
  💡 Suggested fix: Remove `GET /invoices` from the docs, or add the endpoint to the API.
- **POST /users** — Example (curl) `POST /users` sends body field `nickname` that is not in the spec (spec fields: `email`, `name`, `age`).
  `docs/api.md:31` · evidence: `routes in shop-demo#/paths/~1users/post`
  💡 Suggested fix: Remove `nickname` from the example body, or add it to the API.
- **POST /orders** — Example (curl) `POST /orders` omits the required body field `qty`.
  `docs/api.md:45` · evidence: `routes in shop-demo#/paths/~1orders/post/requestBody/properties/qty`
  💡 Suggested fix: Add `qty` to the example body.

### ✅ Verified (11)

<details><summary>Show</summary>

- **GET /users** — `GET /users` exists in the spec.
  `docs/api.md:5` · evidence: `routes in shop-demo#/paths/~1users/get`
- **GET /users** — Parameter `limit` (query) matches the spec.
  `docs/api.md:5` · evidence: `routes in shop-demo#/paths/~1users/get/parameters/0`
- **GET /users** — Parameter `search` (query) matches the spec.
  `docs/api.md:5` · evidence: `routes in shop-demo#/paths/~1users/get/parameters/1`
- **POST /users** — `POST /users` exists in the spec.
  `docs/api.md:15` · evidence: `routes in shop-demo#/paths/~1users/post`
- **POST /users** — Parameter `name` (body) matches the spec.
  `docs/api.md:15` · evidence: `routes in shop-demo#/paths/~1users/post/requestBody/properties/name`
- **POST /users** — Response `201` is defined in the spec.
  `docs/api.md:15` · evidence: `routes in shop-demo#/paths/~1users/post/responses`
- **GET /users/{id}** — `GET /users/{user_id}` exists in the spec (path parameter is named `user_id` in the spec but `id` in the docs).
  `docs/api.md:25` · evidence: `routes in shop-demo#/paths/~1users~1{user_id}/get`
- **POST /orders** — `POST /orders` exists in the spec.
  `docs/api.md:40` · evidence: `routes in shop-demo#/paths/~1orders/post`
- **POST /orders** — Response `201` is defined in the spec.
  `docs/api.md:40` · evidence: `routes in shop-demo#/paths/~1orders/post/responses`
- **GET /health** — `GET /health` exists in the spec.
  `docs/api.md:56` · evidence: `routes in shop-demo#/paths/~1health/get`
- **GET /users/42** — Example (curl) `GET /users/42` matches the spec.
  `docs/api.md:30` · evidence: `routes in shop-demo#/paths/~1users~1{user_id}/get`

</details>

<details><summary>Documentation gaps</summary>

**Required parameters missing from the docs**

- `POST /orders`: required body parameter `item_id` is not documented (docs: docs/api.md:40)
- `POST /orders`: required body parameter `qty` is not documented (docs: docs/api.md:40)

**Spec operations never mentioned in the docs**

- `DELETE /users/{user_id}`

</details>


## 🧭 Project facts check

Checked **24** claim(s) in **3** doc file(s): ✅ 13 verified · ❌ 10 contradicted · ❓ 1 uncertain · health **56.2/100**

#### ❌ Contradicted

- `docs/operations.md:5` (infra) The docs say port 9000 but the project exposes 5432, 8000.
  - Evidence: 5432 at docker-compose.yml:8; 8000 at Dockerfile:5
  - Suggested fix: Use one of the exposed ports (5432, 8000) or expose 9000.
- `docs/operations.md:8` (infra) The docs mention compose service `queue` but no such service is defined (services: api, db).
  - Evidence: searched the docker-compose file(s)
  - Suggested fix: Fix the service name or remove the sentence.
- `docs/operations.md:15` (infra) The docs use Helm value `image.tags` but values.yaml has no such key.
  - Evidence: searched values.yaml
  - Suggested fix: Use `image.tag`.
- `docs/operations.md:16` (infra) The docs use Terraform variable `instance_size` but no `variable "instance_size"` block exists.
  - Evidence: searched *.tf files
  - Suggested fix: Use `instance_type`.
- `docs/setup.md:5` (env) Environment variable `SHOP_DATABASE` is documented but never read by the code (the code reads 2 other variable(s)).
  - Evidence: searched os.environ / os.getenv / process.env / .env.example
  - Suggested fix: Remove `SHOP_DATABASE` from the docs or add support for it in the code.
- `docs/setup.md:8` (env) Environment variable `SHOP_API_TOKEN` is documented but the code reads `SHOP_API_KEY`.
  - Evidence: shop/settings.py:5
  - Suggested fix: Rename `SHOP_API_TOKEN` to `SHOP_API_KEY` in the docs.
- `docs/setup.md:9` (flag) CLI flag `--format` is documented but not defined in the code (2 flag(s) are defined).
  - Evidence: searched argparse / click / commander definitions
  - Suggested fix: Remove `--format` from the docs or add the option to the CLI.
- `docs/setup.md:13` (reference) `build_orders()` is documented but the code defines `build_order` instead.
  - Evidence: shop/cli.py:6
  - Suggested fix: Use `build_order()` in the docs.
- `docs/setup.md:14` (reference) The linked file `deploy.md` does not exist in the repository.
  - Evidence: searched the repository file list
  - Suggested fix: Fix or remove the reference to `deploy.md`.
- `docs/setup.md:3` (version) The docs say Python 3.9+ but the project requires Python 3.11+.
  - Evidence: pyproject.toml:4 (>=3.11)
  - Suggested fix: Change the docs to say Python 3.11+.

<details><summary>❓ Uncertain</summary>

- `docs/setup.md:12` (reference) The docs describe a past change to `shop/config.py`.

</details>


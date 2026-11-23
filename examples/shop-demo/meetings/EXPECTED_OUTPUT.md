## 🗣️ Meeting follow-ups

Source: `meetings/api-sync.vtt` · 11 remark(s) read · 6 decision(s) checked · 1 action item(s)

| Verdict | Decisions |
|---|---|
| ✅ Reflected in code and docs | 1 |
| ❌ Docs no longer match | 2 |
| ❓ Not shipped yet / cannot tell | 3 |

### ❌ Docs that no longer match a decision

- `docs/setup.md:9` The team renamed `--format` to `--output-format` and the code uses `--output-format`, but the docs still say `--format`.
  - Said: “On the CLI side we renamed `--format` to `--output-format` two sprints ago, so that one is done.” (00:00:44, Dana Ortiz)
  - Evidence: meeting at 00:00:44, Dana Ortiz; code: shop/cli.py:13
  - Suggested fix: Replace `--format` with `--output-format` at docs/setup.md:9.
- `docs/api.md:50` The team removed `/invoices` and it is gone from the code, but the docs still describe it.
  - Said: “Billing moved to its own service, so the /invoices endpoint was retired from this API.” (00:01:08, Priya Shah)
  - Evidence: meeting at 00:01:08, Priya Shah; searched the code for `/invoices`
  - Suggested fix: Remove or mark as removed: docs/api.md:50.

### ⏳ Decisions not shipped yet (pending)

- `meetings/api-sync.vtt:7` Rename `SHOP_DB_URL` → `SHOP_DATABASE_URL` is not in the code yet: the code still uses `SHOP_DB_URL`. Docs to update when it ships: docs/setup.md:5.
  - Said: “We agreed last week that SHOP_DB_URL is getting renamed to SHOP_DATABASE_URL so it matches the other services.” (00:00:10, Priya Shah)
  - Evidence: meeting at 00:00:10, Priya Shah; code: shop/settings.py:6
  - Suggested fix: When the rename ships, update the docs from `SHOP_DB_URL` to `SHOP_DATABASE_URL`.
- `meetings/api-sync.vtt:13` Rename `/orders` → `/purchases` is not in the code yet: the code still uses `/orders`. Docs to update when it ships: docs/api.md:40.
  - Said: “Also, we are moving the /orders endpoint to /purchases next quarter, finance asked for the new name.” (00:00:31, Marcus Lee)
  - Evidence: meeting at 00:00:31, Marcus Lee; code: the API definition
  - Suggested fix: When the rename ships, update the docs from `/orders` to `/purchases`.
- `meetings/api-sync.vtt:25` The limit change is not in the code yet: it is still 20.
  - Said: “For list endpoints we are changing the default limit from 20 to 50, but that is behind a flag for now.” (00:01:20, Marcus Lee)
  - Evidence: meeting at 00:01:20, Marcus Lee; code: shop/main.py:27
  - Suggested fix: When it ships, change 20 to 50 in the docs.

### ✅ Decisions already reflected

- `meetings/api-sync.vtt:19` `shop/config.py` is gone from the code and the docs do not present it as current.
  - Said: “And the legacy loader shop/config.py was removed when we switched to settings.py.” (00:00:55, Dana Ortiz)
  - Evidence: meeting at 00:00:55, Dana Ortiz

### 📝 Action items about documentation

- **Priya Shah** (00:01:40): I'll update the setup docs once the variable rename lands.

<details><summary>🧾 Draft decision records</summary>

#### Decision: `SHOP_DB_URL` → `SHOP_DATABASE_URL`
- **Status:** Proposed (not shipped)
- **Raised:** 00:00:10, Priya Shah (`meetings/api-sync.vtt`)
- **Decision:** We agreed last week that SHOP_DB_URL is getting renamed to SHOP_DATABASE_URL so it matches the other services.
- **Docs to update:** When the rename ships, update the docs from `SHOP_DB_URL` to `SHOP_DATABASE_URL`.

#### Decision: `/orders` → `/purchases`
- **Status:** Proposed (not shipped)
- **Raised:** 00:00:31, Marcus Lee (`meetings/api-sync.vtt`)
- **Decision:** Also, we are moving the /orders endpoint to /purchases next quarter, finance asked for the new name.
- **Docs to update:** When the rename ships, update the docs from `/orders` to `/purchases`.

#### Decision: `--format` → `--output-format`
- **Status:** Accepted (in the code)
- **Raised:** 00:00:44, Dana Ortiz (`meetings/api-sync.vtt`)
- **Decision:** On the CLI side we renamed `--format` to `--output-format` two sprints ago, so that one is done.
- **Docs to update:** Replace `--format` with `--output-format` at docs/setup.md:9.

#### Decision: `shop/config.py`
- **Status:** Accepted (in the code)
- **Raised:** 00:00:55, Dana Ortiz (`meetings/api-sync.vtt`)
- **Decision:** And the legacy loader shop/config.py was removed when we switched to settings.py.
- **Docs to update:** none; the docs already agree

#### Decision: `/invoices`
- **Status:** Accepted (in the code)
- **Raised:** 00:01:08, Priya Shah (`meetings/api-sync.vtt`)
- **Decision:** Billing moved to its own service, so the /invoices endpoint was retired from this API.
- **Docs to update:** Remove or mark as removed: docs/api.md:50.

#### Decision: limit: 20 → 50
- **Status:** Proposed (not shipped)
- **Raised:** 00:01:20, Marcus Lee (`meetings/api-sync.vtt`)
- **Decision:** For list endpoints we are changing the default limit from 20 to 50, but that is behind a flag for now.
- **Docs to update:** When it ships, change 20 to 50 in the docs.
</details>


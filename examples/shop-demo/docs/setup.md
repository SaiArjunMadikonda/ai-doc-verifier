# Setup

Requires Python 3.9+.

Set the environment variable `SHOP_API_KEY` and either `SHOP_DB_URL` or `SHOP_DATABASE`.

```bash
export SHOP_API_TOKEN=secret
shopctl --dry-run --format table
```

Settings are loaded in `shop/settings.py`; the legacy loader in `shop/config.py` was removed.
Use `build_order()` or the older `build_orders()` helper to prepare an order, and see
[the API reference](api.md) and [deployment notes](deploy.md).

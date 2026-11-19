"""Runtime settings, read from environment variables."""

import os

API_KEY = os.environ["SHOP_API_KEY"]
DATABASE_URL = os.getenv("SHOP_DB_URL", "sqlite:///shop.db")

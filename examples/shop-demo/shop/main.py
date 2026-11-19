"""A tiny shop API (demo project for the documentation checker)."""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Shop")


class NewUser(BaseModel):
    email: str
    name: str | None = None
    age: int | None = None


class NewOrder(BaseModel):
    item_id: int
    qty: int
    note: str | None = None


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/users")
def list_users(limit: int = 20, search: str | None = None):
    return []


@app.post("/users", status_code=201)
def create_user(user: NewUser):
    return user


@app.get("/users/{user_id}")
def get_user(user_id: int):
    return {"id": user_id}


@app.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int):
    return None


@app.post("/orders", status_code=201)
def create_order(order: NewOrder):
    return order

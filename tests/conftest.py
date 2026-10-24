import re
import pytest
from fastapi.testclient import TestClient

from tuman.config import Settings
from tuman.db import Store
from tuman.web import create_app

TEST_PASSWORD = "Only-for-tests-8672"


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "test.db")
    for role in ("owner", "china", "tajik"):
        db.create_account(role, TEST_PASSWORD, role=role)
    return db


@pytest.fixture
def customer(store):
    return store.create_account("customer", TEST_PASSWORD, name="Муштарӣ")


@pytest.fixture
def app(store):
    return create_app(Settings(db_path=store.path, public_url="http://testserver", web_only=True), store)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def csrf(client, path="/profile"):
    response = client.get(path)
    assert response.status_code == 200
    return re.search(r'data-csrf="([^"]+)"', response.text)[1]


def login(client, login="owner"):
    value = csrf(client, "/login")
    response = client.post("/login", data={"username":login, "password":TEST_PASSWORD, "csrf":value}, follow_redirects=False)
    assert response.status_code == 303
    return csrf(client, "/profile")

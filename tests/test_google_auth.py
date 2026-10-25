import asyncio
import base64
import hashlib
import json
import ssl
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import ClientConnectionError
from fastapi.testclient import TestClient
import pytest

from tuman import google_auth
from tuman.config import Settings
from tuman.db import Store
from tuman.security import digest
from tuman.web import create_app
from .conftest import TEST_PASSWORD, csrf, login

CLIENT_ID = "123456-only-for-tests.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-only-for-tests-123456789"
PROFILE = {"sub": "google-subject-101", "email": "client@example.com", "email_verified": True, "name": "Google Client"}


@pytest.fixture
def google_client(store, monkeypatch):
    settings = Settings(db_path=store.path, public_url="http://testserver", web_only=True, google_client_id=CLIENT_ID, google_client_secret=CLIENT_SECRET)
    app = create_app(settings, store)
    calls = []

    async def exchange(settings, code, verifier):
        calls.append({"code": code, "verifier": verifier})
        if code == "network-error":
            raise google_auth.GoogleOAuthError("Не удалось связаться с Google. Повторите вход позже.")
        if code == "different-subject":
            return {**PROFILE, "sub": "google-subject-202"}
        if code == "new-email":
            return {**PROFILE, "email": "updated@example.com"}
        return dict(PROFILE)

    monkeypatch.setattr(google_auth, "exchange_code", exchange)
    with TestClient(app) as client:
        yield client, calls


def start(client, mode="login"):
    value = csrf(client, "/profile" if mode == "link" else "/login")
    response = client.post("/auth/google", data={"csrf": value, "mode": mode}, follow_redirects=False)
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["location"]).query)


def callback(client, state, code="valid-code", **params):
    return client.get("/auth/google/callback?" + urlencode({"state": state, "code": code, **params}), follow_redirects=False)


def current_account(client, store):
    return store.account(store.session(client.cookies.get("tuman_session"))["account_id"])


def test_google_disabled_until_both_credentials_are_present(client):
    assert "Продолжить с Google" not in client.get("/login").text
    assert client.post("/auth/google").status_code == 404
    assert client.get("/auth/google/callback").status_code == 404
    assert not Settings(google_client_id=CLIENT_ID).google_enabled
    assert not Settings(google_client_secret=CLIENT_SECRET).google_enabled
    assert CLIENT_SECRET not in repr(Settings(google_client_secret=CLIENT_SECRET))


def test_google_start_requires_csrf_and_binds_pkce_to_browser(google_client, store):
    client, calls = google_client
    assert client.post("/auth/google").status_code == 403
    params = start(client)
    state = params["state"][0]
    row = store.one("SELECT * FROM oauth_requests")
    assert row["state_hash"] == digest(state) and row["state_hash"] != state
    assert row["session_id"] == digest(client.cookies.get("tuman_session"))
    assert row["expires"] > time.time()
    assert params["redirect_uri"] == ["http://testserver/auth/google/callback"]
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["code_challenge_method"] == ["S256"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(row["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == [challenge]
    assert row["verifier"] not in str(params)
    page = client.get("/login")
    assert "https://accounts.google.com" in page.headers["content-security-policy"]
    assert CLIENT_SECRET not in page.text
    assert "Продолжить с Google" in client.get("/register").text
    assert not calls


def test_google_creates_customer_and_rotates_session_without_admin_access(google_client, store):
    client, calls = google_client
    params = start(client)
    old_cookie = client.cookies.get("tuman_session")
    response = callback(client, params["state"][0], role="owner", email="attacker@example.com")
    assert response.status_code == 303 and response.headers["location"] == "/account"
    account = current_account(client, store)
    assert account["role"] == "customer" and account["password_hash"] == ""
    assert client.cookies.get("tuman_session") != old_cookie
    assert store.session(old_cookie) is None
    assert store.session(client.cookies.get("tuman_session"))["method"] == "google"
    assert store.one("SELECT email FROM external_identities")["email"] == PROFILE["email"]
    assert client.get("/admin").status_code == 403
    with pytest.raises(ValueError):
        store.authenticate(account["username"], "", "127.0.0.1")
    assert len(calls) == 1


def test_google_returning_subject_uses_same_account_even_if_email_changes(google_client, store):
    client, _ = google_client
    callback(client, start(client)["state"][0])
    aid = current_account(client, store)["id"]
    client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=False)
    assert callback(client, start(client)["state"][0], "new-email").status_code == 303
    assert current_account(client, store)["id"] == aid
    assert store.one("SELECT COUNT(*) n FROM external_identities")["n"] == 1
    assert store.one("SELECT email FROM external_identities")["email"] == "updated@example.com"


def test_google_never_links_accounts_by_matching_email(google_client, store):
    client, _ = google_client
    callback(client, start(client)["state"][0])
    first = current_account(client, store)["id"]
    client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=False)
    callback(client, start(client)["state"][0], "different-subject")
    assert current_account(client, store)["id"] != first
    assert store.one("SELECT COUNT(*) n FROM external_identities WHERE email=?", (PROFILE["email"],))["n"] == 2


def test_google_state_cannot_be_used_from_another_browser(google_client, store):
    client, calls = google_client
    state = start(client)["state"][0]
    with TestClient(client.app) as other:
        other.get("/login")
        assert callback(other, state).status_code == 400
    assert not calls
    assert callback(client, state).status_code == 303
    assert len(calls) == 1


def test_google_callback_replay_is_rejected_before_provider_call(google_client):
    client, calls = google_client
    state = start(client)["state"][0]
    assert callback(client, state).status_code == 303
    assert callback(client, state).status_code == 400
    assert len(calls) == 1


def test_google_expired_flow_is_rejected(google_client, store):
    client, calls = google_client
    state = start(client)["state"][0]
    with store.connection(True) as db:
        db.execute("UPDATE oauth_requests SET expires=0")
    assert callback(client, state).status_code == 400
    assert not calls


@pytest.mark.parametrize("query", ["code=valid-code", "state=one&state=two&code=valid-code"])
def test_google_missing_or_duplicate_state_is_rejected(google_client, query):
    client, calls = google_client
    assert client.get("/auth/google/callback?" + query).status_code == 400
    assert not calls


def test_google_duplicate_code_and_cancelled_login_never_create_accounts(google_client, store):
    client, calls = google_client
    state = start(client)["state"][0]
    assert client.get("/auth/google/callback?" + urlencode({"state": state}) + "&code=one&code=two").status_code == 400
    state = start(client)["state"][0]
    response = client.get("/auth/google/callback?" + urlencode({"state": state, "error": "access_denied", "error_description": "do-not-display"}), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login?")
    assert "do-not-display" not in response.headers["location"]
    assert callback(client, state).status_code == 400
    assert not calls
    assert store.one("SELECT COUNT(*) n FROM external_identities")["n"] == 0


def test_google_provider_failure_consumes_flow_without_creating_account(google_client, store):
    client, calls = google_client
    state = start(client)["state"][0]
    response = callback(client, state, "network-error")
    assert response.status_code == 303 and response.headers["location"].startswith("/login?")
    assert callback(client, state).status_code == 400
    assert len(calls) == 1
    assert store.one("SELECT COUNT(*) n FROM external_identities")["n"] == 0


def test_google_session_revocation_during_exchange_prevents_login(google_client, store, monkeypatch):
    client, _ = google_client
    state = start(client)["state"][0]
    sid = digest(client.cookies.get("tuman_session"))

    async def revoked(settings, code, verifier):
        with store.connection(True) as db:
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        return dict(PROFILE)

    monkeypatch.setattr(google_auth, "exchange_code", revoked)
    assert callback(client, state).status_code == 400
    assert store.one("SELECT COUNT(*) n FROM external_identities")["n"] == 0


def test_staff_cannot_link_google_or_gain_staff_role_from_google(google_client, store):
    client, calls = google_client
    token = login(client)
    assert client.post("/auth/google", data={"csrf": token, "mode": "link"}).status_code == 403
    assert not calls
    client.post("/logout", data={"csrf": token}, follow_redirects=False)
    callback(client, start(client)["state"][0])
    aid = current_account(client, store)["id"]
    client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=False)
    with store.connection(True) as db:
        db.execute("UPDATE accounts SET role='owner' WHERE id=?", (aid,))
    assert callback(client, start(client)["state"][0]).status_code == 400
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_customer_can_link_google_preserving_parcels_and_telegram(google_client, store, customer):
    client, _ = google_client
    tg = store.telegram_person(202, "TG Client", language="ru")
    store.exchange_link(store.issue_link(person_id=tg["id"]), customer["id"], True)
    store.add_track(customer["person_id"], "GOOGLE123")
    claim = store.one("SELECT id FROM claims")
    owner = store.one("SELECT id FROM accounts WHERE role='owner'")
    store.decide_claim(claim["id"], owner["id"], True)
    login(client, "customer")
    assert callback(client, start(client, "link")["state"][0]).status_code == 303
    assert current_account(client, store)["id"] == customer["id"]
    assert store.parcel("GOOGLE123")["owner_id"] == customer["person_id"]
    assert store.one("SELECT telegram_id FROM people WHERE id=?", (customer["person_id"],))["telegram_id"] == 202
    assert store.one("SELECT COUNT(*) n FROM accounts WHERE role='customer'")["n"] == 1
    client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=False)
    callback(client, start(client)["state"][0])
    assert current_account(client, store)["id"] == customer["id"]


def test_google_link_cannot_take_over_another_account(google_client, store, customer):
    client, _ = google_client
    callback(client, start(client)["state"][0])
    original = current_account(client, store)["id"]
    client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=False)
    login(client, "customer")
    assert callback(client, start(client, "link")["state"][0]).status_code == 400
    assert current_account(client, store)["id"] == customer["id"]
    assert store.one("SELECT account_id FROM external_identities")["account_id"] == original


def test_passwordless_customer_can_set_first_password_only_after_recent_google_login(google_client, store):
    client, _ = google_client
    callback(client, start(client)["state"][0])
    account = current_account(client, store)
    token = csrf(client)
    assert 'name="current"' not in client.get("/profile").text
    response = client.post("/profile/credentials", data={"csrf": token, "username": "google_client", "password": TEST_PASSWORD, "confirm": TEST_PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    assert store.authenticate("google_client", TEST_PASSWORD, "127.0.0.1")["id"] == account["id"]
    token = csrf(client)
    assert client.post("/profile/credentials", data={"csrf": token, "username": "renamed", "password": TEST_PASSWORD, "confirm": TEST_PASSWORD}).status_code == 400


@pytest.mark.parametrize("method,age", [("google", 601), ("telegram", 0)])
def test_first_password_rejects_stale_or_non_google_session(google_client, store, method, age):
    client, _ = google_client
    callback(client, start(client)["state"][0])
    token = csrf(client)
    account = current_account(client, store)
    with store.connection(True) as db:
        db.execute("UPDATE sessions SET created=?,method=? WHERE id=?", (int(time.time())-age, method, digest(client.cookies.get("tuman_session"))))
    assert client.post("/profile/credentials", data={"csrf": token, "username": "renamed", "password": TEST_PASSWORD, "confirm": TEST_PASSWORD}).status_code == 400
    assert store.account(account["id"])["password_hash"] == ""


def test_google_schema_upgrade_keeps_existing_accounts_parcels_and_sessions(store, customer):
    store.add_track(customer["person_id"], "MIGRATE123")
    token = store.new_session(customer["id"])
    with store.connection(True) as db:
        db.execute("DROP TABLE oauth_requests")
        db.execute("DROP TABLE external_identities")
        db.execute("PRAGMA user_version=1")
    upgraded = Store(store.path)
    assert upgraded.account(customer["id"])["username"] == "customer"
    assert upgraded.parcel("MIGRATE123")
    assert upgraded.session(token)["account_id"] == customer["id"]
    assert upgraded.one("PRAGMA user_version")["user_version"] == 2


@pytest.mark.parametrize("change", [{"sub": ""}, {"sub": None}, {"email_verified": False}, {"email_verified": "true"}, {"email": "invalid"}])
def test_google_profile_requires_subject_and_verified_email(change):
    with pytest.raises(google_auth.GoogleOAuthError):
        google_auth.verified_profile({**PROFILE, **change})


class FakeContent:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        for index in range(0, len(self.body), size):
            yield self.body[index:index+size]


class FakeResponse:
    def __init__(self, data, status=200):
        self.status = status
        self.content = FakeContent(json.dumps(data).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def transport(monkeypatch, token_data=None, status=200, failure=None, profile=None, userinfo_status=200):
    requests = []

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]
            assert kwargs["trust_env"] is True
            assert self.connector._ssl.verify_mode == ssl.CERT_REQUIRED

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def post(self, url, **kwargs):
            requests.append(("POST", url, kwargs))
            if failure:
                raise failure
            return FakeResponse(token_data if token_data is not None else {"access_token": "provider-access", "token_type": "Bearer", "id_token": "untrusted-ignored-value"}, status)

        def get(self, url, **kwargs):
            requests.append(("GET", url, kwargs))
            return FakeResponse(PROFILE if profile is None else profile, userinfo_status)

    monkeypatch.setattr(google_auth, "ClientSession", Session)
    return requests


def test_google_transport_uses_fixed_https_endpoints_pkce_and_authenticated_userinfo(monkeypatch):
    requests = transport(monkeypatch)
    settings = Settings(public_url="https://cargo.example", google_client_id=CLIENT_ID, google_client_secret=CLIENT_SECRET)
    profile = asyncio.run(google_auth.exchange_code(settings, "provider-code", "pkce-verifier"))
    assert profile["sub"] == PROFILE["sub"]
    assert requests[0][0:2] == ("POST", google_auth.TOKEN_URL)
    assert requests[0][2]["data"]["client_secret"] == CLIENT_SECRET
    assert requests[0][2]["data"]["code_verifier"] == "pkce-verifier"
    assert requests[0][2]["data"]["redirect_uri"] == "https://cargo.example/auth/google/callback"
    assert requests[1][0:2] == ("GET", google_auth.USERINFO_URL)
    assert requests[1][2]["headers"] == {"Authorization": "Bearer provider-access"}
    assert all(request[2]["allow_redirects"] is False for request in requests)


def test_google_transport_does_not_follow_redirects_or_leak_network_secrets(monkeypatch):
    requests = transport(monkeypatch, status=302)
    with pytest.raises(google_auth.GoogleOAuthError):
        asyncio.run(google_auth.exchange_code(Settings(), "provider-code", "verifier"))
    assert len(requests) == 1
    transport(monkeypatch, failure=ClientConnectionError("sensitive-code-and-client-secret"))
    with pytest.raises(google_auth.GoogleOAuthError) as error:
        asyncio.run(google_auth.exchange_code(Settings(), "provider-code", "verifier"))
    assert "sensitive-code" not in str(error.value)


def test_google_transport_rejects_header_injection_and_oversized_response(monkeypatch):
    requests = transport(monkeypatch, token_data={"access_token": "bad\r\nInjected: yes", "token_type": "Bearer"})
    with pytest.raises(google_auth.GoogleOAuthError):
        asyncio.run(google_auth.exchange_code(Settings(), "provider-code", "verifier"))
    assert len(requests) == 1
    transport(monkeypatch, token_data={"access_token": "x"*65000, "token_type": "Bearer"})
    with pytest.raises(google_auth.GoogleOAuthError):
        asyncio.run(google_auth.exchange_code(Settings(), "provider-code", "verifier"))


@pytest.mark.parametrize("code,status", [("invalid_client", 401), ("invalid_grant", 400), ("redirect_uri_mismatch", 400), ("unauthorized_client", 401), ("invalid_request", 400)])
def test_google_transport_reports_only_allowlisted_provider_errors(monkeypatch, caplog, code, status):
    secret = "must-never-appear-in-diagnostics"
    transport(monkeypatch, status=status, token_data={"error": code, "error_description": secret, "access_token": secret, "client_secret": secret})
    with pytest.raises(google_auth.GoogleOAuthError) as error:
        asyncio.run(google_auth.exchange_code(Settings(), secret, "verifier"))
    assert code in str(error.value)
    assert secret not in str(error.value)
    assert secret not in caplog.text
    assert f"stage=token status={status} error={code}" in caplog.text


def test_google_transport_does_not_echo_unknown_provider_error(monkeypatch, caplog):
    secret = "private-response-content"
    transport(monkeypatch, status=403, token_data={"error": secret, "error_description": secret})
    with pytest.raises(google_auth.GoogleOAuthError) as error:
        asyncio.run(google_auth.exchange_code(Settings(), "code", "verifier"))
    assert "HTTP 403" in str(error.value)
    assert secret not in str(error.value) and secret not in caplog.text
    assert "error=unknown" in caplog.text


def test_google_transport_reports_userinfo_stage_safely(monkeypatch, caplog):
    transport(monkeypatch, profile={"error": "invalid_token", "error_description": "private-token"}, userinfo_status=401)
    with pytest.raises(google_auth.GoogleOAuthError, match="invalid_token"):
        asyncio.run(google_auth.exchange_code(Settings(), "code", "verifier"))
    assert "stage=userinfo status=401 error=invalid_token" in caplog.text
    assert "private-token" not in caplog.text


def test_google_login_failure_is_rendered_as_warning(google_client):
    client, _ = google_client
    state = start(client)["state"][0]
    response = callback(client, state, "network-error")
    assert "notice_type=error" in response.headers["location"]
    page = client.get(response.headers["location"])
    assert 'class="notice warning" role="status"' in page.text
    assert 'class="notice success" role="status"' not in page.text

"""Browser OAuth flow with a simulated Google page and an isolated test DB."""
from html import escape
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

from tuman.db import Store
from .conftest import TEST_PASSWORD


def run():
    root = Path(__file__).resolve().parent.parent
    artifacts = root / "test-results"
    artifacts.mkdir(exist_ok=True)
    report = {"scenarios": [], "passed": False}
    errors = []
    with tempfile.TemporaryDirectory(prefix="tuman-google-browser-") as temporary:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        path = Path(temporary) / "google.db"
        env = {**os.environ, "TUMAN_E2E_DB": str(path), "TUMAN_E2E_PORT": str(port), "TUMAN_E2E_GOOGLE": "1"}
        with (artifacts / "google-browser-server.log").open("w") as log:
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "tests.browser_app:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                ready = False
                for _ in range(100):
                    try:
                        with urlopen(base + "/health", timeout=1) as response:
                            ready = response.status == 200
                        if ready:
                            break
                    except OSError:
                        time.sleep(.1)
                assert ready, "Google browser test server failed to start"
                store = Store(path)
                existing = store.create_account("existing_client", TEST_PASSWORD, "Existing Client")
                store.add_track(existing["person_id"], "BROWSER123")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or shutil.which("chromium"), headless=True, args=["--no-sandbox"])

                    def context(code, **kwargs):
                        context = browser.new_context(**kwargs)
                        context.on("page", lambda p: p.on("pageerror", lambda error: errors.append(str(error))))

                        def provider(route):
                            params = parse_qs(urlsplit(route.request.url).query)
                            assert params["code_challenge_method"] == ["S256"]
                            assert params["redirect_uri"] == [base + "/auth/google/callback"]
                            callback = params["redirect_uri"][0] + "?" + urlencode({"state": params["state"][0], "code": code})
                            route.fulfill(status=200, content_type="text/html", body='<html><body><a id="continue" href="' + escape(callback, quote=True) + '">Continue</a></body></html>')

                        context.route("https://accounts.google.com/**", provider)
                        return context

                    def finish_google(page):
                        page.locator('form[action="/auth/google"] button').click()
                        expect(page.locator("#continue")).to_be_visible()
                        page.locator("#continue").click()

                    def logout(page):
                        page.goto(base + "/profile")
                        value = page.locator("body").get_attribute("data-csrf")
                        response = page.request.post(base + "/logout", form={"csrf": value})
                        assert response.ok

                    desktop = context("new-client", viewport={"width": 1365, "height": 1080})
                    page = desktop.new_page()
                    page.goto(base + "/register")
                    expect(page.locator(".google-button")).to_be_visible()
                    page.screenshot(path=str(artifacts / "google-login-desktop.png"), full_page=True)
                    finish_google(page)
                    expect(page).to_have_url(base + "/account")
                    linked = store.one("SELECT account_id FROM external_identities WHERE subject='browser-new-client'")["account_id"]
                    assert store.account(linked)["role"] == "customer"
                    report["scenarios"].append("Google registration completes a real cross-site redirect with session cookie and PKCE")

                    page.goto(base + "/profile")
                    expect(page.locator('input[name="current"]')).to_have_count(0)
                    page.locator('input[name="username"]').fill("google_browser")
                    page.locator('input[name="password"]').fill(TEST_PASSWORD)
                    page.locator('input[name="confirm"]').fill(TEST_PASSWORD)
                    page.locator('form[action="/profile/credentials"] button').click()
                    expect(page.locator('input[name="current"]')).to_have_count(1)
                    logout(page)
                    page.goto(base + "/login")
                    finish_google(page)
                    expect(page).to_have_url(base + "/account")
                    assert store.one("SELECT COUNT(*) n FROM external_identities WHERE subject='browser-new-client'")["n"] == 1
                    assert store.account(linked)["username"] == "google_browser"
                    report["scenarios"].append("Google customer sets an optional local password and returns to the same account")

                    customer_context = context("existing-client", viewport={"width": 1365, "height": 1080})
                    customer = customer_context.new_page()
                    customer.goto(base + "/login")
                    customer.locator('input[name="username"]').fill("existing_client")
                    customer.locator('input[name="password"]').fill(TEST_PASSWORD)
                    customer.locator('form[action="/login"] button').click()
                    expect(customer).to_have_url(base + "/account")
                    customer.goto(base + "/profile")
                    finish_google(customer)
                    expect(customer.locator(".notice.success").filter(has_text="existing-client@example.com")).to_be_visible()
                    assert store.one("SELECT account_id FROM external_identities WHERE subject='browser-existing-client'")["account_id"] == existing["id"]
                    assert store.one("SELECT person_id FROM claims WHERE parcel_id=(SELECT id FROM parcels WHERE track='BROWSER123')")["person_id"] == existing["person_id"]
                    report["scenarios"].append("Existing password customer links Google through profile while retaining parcel requests")

                    mobile_context = context("new-client", viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                    mobile = mobile_context.new_page()
                    mobile.goto(base + "/login")
                    expect(mobile.locator(".google-button")).to_be_visible()
                    assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    mobile.screenshot(path=str(artifacts / "google-login-mobile.png"), full_page=True)
                    finish_google(mobile)
                    expect(mobile).to_have_url(base + "/account")
                    report["scenarios"].append("Mobile Google button has no horizontal overflow and logs into the same account")
                    assert not errors, errors
                    report["page_errors"] = errors
                    report["passed"] = True
                    browser.close()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
                (artifacts / "google-browser-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    run()

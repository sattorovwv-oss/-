"""Run via python -m tests.browser_flow. Uses an isolated DB and a real Chromium."""
from pathlib import Path
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import uuid

from playwright.sync_api import sync_playwright, expect

from tuman.db import Store
from .conftest import TEST_PASSWORD


def run():
    root=Path(__file__).resolve().parent.parent
    artifacts=root/"test-results"
    artifacts.mkdir(exist_ok=True)
    report={"run_id":str(uuid.uuid4()),"scenarios":[],"passed":False}
    errors=[]
    with tempfile.TemporaryDirectory(prefix="tuman-browser-") as temporary:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0)); port=sock.getsockname()[1]
        base=f"http://127.0.0.1:{port}"
        env={**os.environ,"TUMAN_E2E_DB":str(Path(temporary)/"browser.db"),"TUMAN_E2E_PORT":str(port)}
        server_log=open(artifacts/"browser-server.log","w")
        server=subprocess.Popen([sys.executable,"-m","uvicorn","tests.browser_app:app","--host","127.0.0.1","--port",str(port),"--no-access-log"],cwd=root,env=env,stdout=server_log,stderr=subprocess.STDOUT)
        try:
            ready=False
            for _ in range(100):
                try:
                    with urlopen(base+"/health",timeout=1) as response:
                        ready=response.status==200
                    if ready: break
                except OSError: time.sleep(.1)
            assert ready,"Browser test server did not start"
            store=Store(Path(temporary)/"browser.db")
            with sync_playwright() as playwright:
                chromium=os.environ.get("CHROMIUM_PATH") or shutil.which("chromium")
                browser=playwright.chromium.launch(executable_path=chromium,headless=True,args=["--no-sandbox"])
                def context(**kwargs):
                    c=browser.new_context(**kwargs)
                    c.on("page",lambda p:p.on("pageerror",lambda error:errors.append(str(error))))
                    return c
                def login(page,name="china"):
                    page.goto(base+"/login")
                    page.locator('[name="username"]').fill(name)
                    page.locator('[name="password"]').fill(TEST_PASSWORD)
                    page.locator('form button[type="submit"]').click()
                    expect(page).to_have_url(base+("/account" if name=="webclient" else "/admin"))
                china_context=context(viewport={"width":1440,"height":1000})
                china=china_context.new_page(); login(china)
                china.goto(base+"/admin/scan")
                for i in range(12):
                    china.locator("#track-input").fill(f"FAST{i:06}")
                    china.locator("#track-input").press("Enter")
                expect(china.locator("#scan-ok")).to_have_text("12",timeout=15000)
                assert store.one("SELECT COUNT(*) n FROM parcels")["n"]==12
                report["scenarios"].append("12 rapid scanner inputs saved once each")
                china_context.set_offline(True)
                china.locator("#track-input").fill("OFFLINE123")
                china.locator("#track-input").press("Enter")
                expect(china.locator("#scan-pending")).to_have_text("1")
                assert store.parcel("OFFLINE123") is None
                china_context.route("**/api/scans",lambda route:route.abort())
                china_context.set_offline(False)
                china.reload()
                expect(china.locator("#scan-pending")).to_have_text("1")
                china_context.unroute("**/api/scans")
                china.locator("#retry-scans").click()
                expect(china.locator("#scan-pending")).to_have_text("0",timeout=15000)
                assert store.parcel("OFFLINE123")["stage"]==10
                report["scenarios"].append("offline queue survives reload and is delivered after reconnect")
                china.screenshot(path=str(artifacts/"scanner.png"),full_page=True)

                customer_context=context(viewport={"width":1365,"height":1000})
                customer=customer_context.new_page(); customer.goto(base+"/register")
                customer.locator('[name="name"]').fill("Фарид")
                customer.locator('[name="username"]').fill("webclient")
                customer.locator('[name="password"]').fill(TEST_PASSWORD)
                customer.locator('[name="confirm"]').fill(TEST_PASSWORD)
                customer.locator('form button[type="submit"]').click()
                expect(customer).to_have_url(base+"/account")
                customer.locator('[name="track"]').fill("FAST000001")
                customer.locator('form[action="/account/tracks"] button').click()
                expect(customer.locator(".request-list")).to_contain_text("FAST000001")
                assert not customer.locator("table").count()
                china.goto(base+"/admin/claims")
                china.on("dialog",lambda dialog:dialog.accept())
                china.locator('article').filter(has_text="FAST000001").locator('button[value="approve"]').click()
                customer.reload()
                expect(customer.locator("table")).to_contain_text("FAST000001")
                report["scenarios"].append("customer registration, claim submission and warehouse approval")

                tajik_context=context(); tajik=tajik_context.new_page(); login(tajik,"tajik")
                tajik.goto(base+"/admin/scan"); tajik.locator("#track-input").fill("FAST000001"); tajik.locator("#track-input").press("Enter")
                expect(tajik.locator("#scan-ok")).to_have_text("1")
                customer.reload(); expect(customer.locator("table")).to_contain_text("Дар анбори Тоҷикистон")
                china.goto(base+"/admin/scan"); china.locator("#track-input").fill("FAST000001"); china.locator("#track-input").press("Enter")
                expect(china.locator("#scan-log")).to_contain_text("Посылка уже дальше по маршруту")
                assert store.parcel("FAST000001")["stage"]==30
                report["scenarios"].append("Tajik arrival updates customer status; late China scan cannot regress it")

                person=store.telegram_person(987654,"Фарид",language="ru")
                token=store.issue_link(person_id=person["id"])
                customer.on("dialog",lambda dialog:dialog.accept())
                customer.goto(base+"/auth/telegram#token="+token)
                expect(customer).to_have_url(base+"/account")
                expect(customer.locator(".notice.success")).to_contain_text("Telegram пайваст аст")
                assert "token=" not in customer.url
                report["scenarios"].append("Telegram identity links to signed-in customer without losing parcels")

                china_account=store.one("SELECT id FROM accounts WHERE role='china'")
                link=store.issue_link(account_id=china_account["id"])
                magic_context=context(); magic=magic_context.new_page()
                magic.goto(base+"/auth/telegram#token="+link)
                expect(magic).to_have_url(base+"/admin")
                report["scenarios"].append("admin one-click Telegram login without password prompt")

                china.goto(base+"/profile")
                expect(china.locator(".session-row")).to_have_count(2)
                china.locator('input[value="others"]').locator("..").locator("button").click()
                magic.goto(base+"/admin"); expect(magic).to_have_url(base+"/login")
                report["scenarios"].append("admin can terminate other active sessions")

                owner_context=context(viewport={"width":1440,"height":1000}); owner=owner_context.new_page(); login(owner,"owner")
                owner.screenshot(path=str(artifacts/"dashboard.png"),full_page=True)
                mobile_context=context(viewport={"width":390,"height":844},is_mobile=True,device_scale_factor=1)
                mobile=mobile_context.new_page(); login(mobile,"webclient")
                assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),"Mobile page overflows viewport"
                mobile.screenshot(path=str(artifacts/"customer-mobile.png"),full_page=True)
                report["scenarios"].append("desktop dashboard and mobile customer layout render without page errors")
                assert not errors,errors
                browser.close()
                report["passed"]=True
        finally:
            server.terminate()
            try: server.wait(timeout=15)
            except subprocess.TimeoutExpired: server.kill(); server.wait()
            server_log.close()
            report["browser_errors"]=errors
            (artifacts/"browser-report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":
    run()

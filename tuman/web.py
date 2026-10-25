from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from io import BytesIO
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import ROOT, Settings
from . import google_auth
from .db import ConfirmTelegramLink, Store
from .security import digest, display, track_code
from .texts import status

COOKIE = "tuman_session"
# The product specifies a fixed UTC+5 offset, without configurable DST rules.
# Windows does not ship the IANA timezone database used by ZoneInfo.
DUSHANBE_TIMEZONE = timezone(timedelta(hours=5), name="Asia/Dushanbe")
ROLES = {"owner": "Соҳиби система", "china": "Анбори Чин", "tajik": "Анбори Тоҷикистон", "customer": "Ҳисоби шахсӣ"}


def create_app(settings=None, store=None):
    settings = settings or Settings.from_env()
    store = store or Store(settings.db_path)
    store.bootstrap(settings)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.settings = store, settings
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    templates.env.filters["date"] = lambda v: datetime.fromtimestamp(v, DUSHANBE_TIMEZONE).strftime("%d.%m.%Y %H:%M") if v else "—"
    templates.env.globals.update(status=status, display=display, role_name=ROLES)
    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

    def identity(request, customer=None):
        account = request.state.account
        if not account:
            raise HTTPException(401, "Сначала войдите в аккаунт / Аввал ворид шавед")
        if customer is True and account["role"] != "customer":
            raise HTTPException(403, "Клиентский раздел / Қисми муштарӣ")
        if customer is False and account["role"] == "customer":
            raise HTTPException(403, "Требуется доступ сотрудника / Дастрасии корманд лозим")
        return account

    def owner(request):
        account = identity(request, False)
        if account["role"] != "owner":
            raise HTTPException(403, "Только владелец / Танҳо соҳиб")
        return account

    def set_cookie(response, token):
        response.set_cookie(COOKIE, token, max_age=43200, httponly=True, secure=settings.production, samesite="lax", path="/")
        return response

    def redirect(path, message=""):
        if message:
            from urllib.parse import urlencode
            path += ("&" if "?" in path else "?") + urlencode({"notice": message})
        return RedirectResponse(path, status_code=303)

    def view(request, template, **data):
        token = None
        session = request.state.session
        if not session:
            token = store.new_session(None, request.headers.get("user-agent", ""), request.client.host if request.client else "")
            session = store.session(token)
        account = request.state.account
        context = {"request": request, "account": account, "session": session, "csrf": session["csrf"], "prefs": store.preferences(), "web_only": settings.web_only, "google_enabled": settings.google_enabled, "notice": request.query_params.get("notice", "")[:200], **data}
        response = templates.TemplateResponse(request=request, name=template, context=context)
        if token:
            set_cookie(response, token)
        return response

    def check_csrf(request, value):
        session = request.state.session
        if not session or not value or not secrets.compare_digest(session["csrf"], str(value)):
            raise HTTPException(403, "Страница устарела. Обновите и повторите. / Саҳифаро нав кунед.")

    async def form(request):
        data = await request.form(max_files=0, max_fields=16, max_part_size=16000)
        check_csrf(request, data.get("csrf"))
        return data

    @app.middleware("http")
    async def security_headers(request, call_next):
        request.state.session = await run_in_threadpool(store.session, request.cookies.get(COOKIE))
        request.state.account = await run_in_threadpool(store.account, request.state.session["account_id"]) if request.state.session and request.state.session["account_id"] else None
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid request length"}, 400)
        if size > 64000:
            return JSONResponse({"detail": "Request too large"}, 413)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'", "Permissions-Policy": "camera=(), microphone=(), geolocation=()"})
        if settings.google_enabled:
            # Chrome also checks the redirect following the same-origin POST.
            response.headers["Content-Security-Policy"] += " https://accounts.google.com"
        if settings.production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc)}, status_code=400)
        response = view(request, "error.html", message=str(exc))
        response.status_code = 400
        return response

    @app.exception_handler(PermissionError)
    async def denied(request, exc):
        return JSONResponse({"detail": str(exc)}, 403) if request.url.path.startswith("/api/") else HTMLResponse("Доступ запрещён / Дастрасӣ манъ аст", 403)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        if exc.status_code == 401 and not request.url.path.startswith("/api/"):
            return redirect("/login")
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)
        response = view(request, "error.html", message=str(exc.detail))
        response.status_code = exc.status_code
        return response

    @app.get("/health")
    def health():
        store.one("SELECT 1 AS ready")
        return {"status": "ok", "telegram": "disabled" if settings.web_only else "configured" if settings.bot_token else "missing"}

    @app.get("/")
    def home(request: Request):
        if request.state.account:
            return redirect("/account" if request.state.account["role"] == "customer" else "/admin")
        return view(request, "landing.html")

    @app.get("/login")
    def login_page(request: Request):
        return view(request, "auth.html", register=False)

    @app.get("/register")
    def register_page(request: Request):
        return view(request, "auth.html", register=True)

    @app.post("/register")
    async def register(request: Request):
        data = await form(request)
        ip = request.client.host if request.client else ""
        if store.limited("register:"+digest(ip), limit=5, seconds=3600):
            raise HTTPException(429, "Слишком много регистраций. Повторите позже.")
        if data.get("password") != data.get("confirm"):
            raise ValueError("Пароли не совпадают / Рамзҳо яксон нестанд")
        account = await run_in_threadpool(store.create_account, str(data.get("username", "")), str(data.get("password", "")), str(data.get("name", "")))
        if request.state.session:
            store.revoke_session(request.state.session["id"], request.state.session["account_id"])
        token = store.new_session(account["id"], request.headers.get("user-agent", ""), ip)
        return set_cookie(redirect("/account"), token)

    @app.post("/login")
    async def login(request: Request):
        data = await form(request)
        account = await run_in_threadpool(store.authenticate, str(data.get("username", "")), str(data.get("password", "")), request.client.host if request.client else "")
        if request.state.session:
            with store.connection(True) as db:
                db.execute("DELETE FROM sessions WHERE id=?", (request.state.session["id"],))
        token = store.new_session(account["id"], request.headers.get("user-agent", ""), request.client.host if request.client else "")
        return set_cookie(redirect("/account" if account["role"] == "customer" else "/admin"), token)

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        with store.connection(True) as db:
            db.execute("DELETE FROM sessions WHERE id=?", (request.state.session["id"],))
        response = redirect("/")
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.post("/auth/google")
    async def google_start(request: Request):
        if not settings.google_enabled:
            raise HTTPException(404, "Вход через Google пока не подключён.")
        data = await form(request)
        mode = str(data.get("mode", "login"))
        if mode not in ("login", "link"):
            raise ValueError("Некорректный способ входа.")
        current = request.state.account
        if current and current["role"] != "customer":
            raise HTTPException(403, "Google подключается к клиентскому аккаунту. Для сотрудников используйте текущий способ входа.")
        if mode == "link":
            current = identity(request, True)
        elif current:
            return redirect("/profile", "Для подключения Google используйте кнопку в профиле.")
        ip = request.client.host if request.client else ""
        if store.limited("google-start:" + digest(ip), limit=20, seconds=600):
            raise HTTPException(429, "Слишком много попыток входа. Повторите позже.")
        state, verifier = store.begin_google_auth(request.state.session["id"], current["id"] if current else None)
        return RedirectResponse(google_auth.authorization_url(settings, state, verifier), status_code=303)

    @app.get("/auth/google/callback")
    async def google_callback(request: Request):
        if not settings.google_enabled:
            raise HTTPException(404, "Вход через Google пока не подключён.")
        params = request.query_params
        if len(params.getlist("state")) != 1:
            raise ValueError("Ссылка входа недействительна. Начните вход через Google заново.")
        session = request.state.session
        flow = store.consume_google_auth(params.get("state"), session["id"] if session else None)
        target = "/profile" if flow["account_id"] is not None else "/login"
        if "error" in params:
            if len(params.getlist("error")) != 1 or "code" in params:
                raise ValueError("Некорректный ответ Google. Повторите вход.")
            return redirect(target, "Вход через Google отменён. Вы можете попробовать ещё раз.")
        code = params.get("code", "")
        if len(params.getlist("code")) != 1 or not 1 <= len(code) <= 4096 or any(ord(char) < 32 for char in code):
            raise ValueError("Некорректный ответ Google. Повторите вход.")
        try:
            profile = await google_auth.exchange_code(settings, code, flow["verifier"])
        except google_auth.GoogleOAuthError as exc:
            return redirect(target, str(exc))
        account, token = store.complete_google_auth(flow, profile, request.headers.get("user-agent", ""), request.client.host if request.client else "")
        return set_cookie(redirect("/profile", "Google подключён к вашему аккаунту.") if flow["account_id"] is not None else redirect("/account"), token)

    @app.get("/auth/telegram")
    def telegram_page(request: Request):
        return view(request, "telegram.html")

    @app.post("/api/auth/telegram")
    async def telegram_exchange(request: Request):
        origin = request.headers.get("origin")
        expected = settings.public_url
        if origin != expected:
            raise HTTPException(403, "Invalid origin")
        payload = await request.json()
        token_value = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token_value, str):
            raise ValueError("Ссылка недействительна")
        current = request.state.account
        try:
            account = store.exchange_link(token_value, current["id"] if current else None, payload.get("confirm") is True)
        except ConfirmTelegramLink as exc:
            return JSONResponse({"needs_confirmation": True, "name": exc.name, "telegram_id": exc.telegram_id}, status_code=202)
        if request.state.session:
            with store.connection(True) as db:
                db.execute("DELETE FROM sessions WHERE id=?", (request.state.session["id"],))
        token = store.new_session(account["id"], request.headers.get("user-agent", ""), request.client.host if request.client else "", method="telegram")
        response = JSONResponse({"redirect": "/account" if account["role"] == "customer" else "/admin"})
        return set_cookie(response, token)

    @app.get("/account")
    def customer_account(request: Request):
        account = identity(request, True)
        person = store.one("SELECT * FROM people WHERE id=?", (account["person_id"],))
        parcels = store.list_parcels(person_id=person["id"], limit=100)
        claims = store.rows("SELECT c.*,p.track,p.stage FROM claims c JOIN parcels p ON p.id=c.parcel_id WHERE c.person_id=? AND p.deleted=0 ORDER BY c.id DESC LIMIT 100", (person["id"],))
        tickets = store.rows("SELECT * FROM tickets WHERE person_id=? ORDER BY id DESC LIMIT 30", (person["id"],))
        return view(request, "customer.html", person=person, parcels=parcels, claims=claims, tickets=tickets)

    @app.post("/account/tracks")
    async def customer_add(request: Request):
        account = identity(request, True)
        data = await form(request)
        if store.limited(f"tracks:{account['id']}", limit=50, seconds=3600):
            raise HTTPException(429, "Слишком много заявок")
        result = store.add_track(account["person_id"], str(data.get("track", "")))
        return redirect("/account", "Заявка отправлена / Дархост фиристода шуд" if result == "pending" else "Посылка уже привязана / Бор аллакай пайваст аст" if result == "duplicate" else "Трек занят. Обратитесь в поддержку.")

    @app.post("/account/support")
    async def customer_support(request: Request):
        account = identity(request, True)
        data = await form(request)
        if store.limited(f"support:{account['id']}", limit=10, seconds=3600):
            raise HTTPException(429, "Слишком много обращений")
        store.create_ticket(account["person_id"], str(data.get("text", "")))
        return redirect("/account", "Обращение сохранено / Дархост сабт шуд")

    @app.get("/account/search")
    def customer_search(request: Request, track: str = ""):
        account = identity(request, True)
        parcel = store.parcel(track) if track else None
        return view(request, "search.html", parcel=parcel, searched=bool(track), own=parcel and parcel["owner_id"] == account["person_id"])

    @app.get("/profile")
    def profile(request: Request):
        account = identity(request)
        sessions = store.rows("SELECT id,created,seen,expires,agent,ip,method FROM sessions WHERE account_id=? AND expires>? ORDER BY seen DESC", (account["id"], int(time.time())))
        google_identity = store.one("SELECT email FROM external_identities WHERE provider='google' AND account_id=?", (account["id"],))
        return view(request, "profile.html", sessions=sessions, google_identity=google_identity)

    @app.post("/profile/credentials")
    async def credentials(request: Request):
        account = identity(request)
        data = await form(request)
        if data.get("password", "") != data.get("confirm", ""):
            raise ValueError("Пароли не совпадают / Рамзҳо яксон нестанд")
        if store.limited(f"credentials:{account['id']}", limit=8):
            raise HTTPException(429, "Повторите позже")
        await run_in_threadpool(store.change_credentials, account["id"], str(data.get("current", "")), str(data.get("username", "")), str(data.get("password", "")), request.state.session["id"])
        return redirect("/profile", "Данные изменены. Другие сеансы завершены. / Дигар сессияҳо баста шуданд.")

    @app.post("/profile/sessions/revoke")
    async def revoke(request: Request):
        account = identity(request)
        data = await form(request)
        sid = str(data.get("session_id", ""))
        if sid == "others":
            with store.connection(True) as db:
                db.execute("DELETE FROM sessions WHERE account_id=? AND id<>?", (account["id"], request.state.session["id"]))
        else:
            store.revoke_session(sid, account["id"])
        return redirect("/profile", "Сеанс завершён / Сессия баста шуд")

    @app.get("/admin")
    def dashboard(request: Request):
        account = identity(request, False)
        stats = store.stats()
        recent = store.list_parcels(warehouse=account["role"], limit=8)
        return view(request, "dashboard.html", stats=stats, parcels=recent)

    @app.get("/admin/scan")
    def scan_page(request: Request, warehouse: str = ""):
        account = identity(request, False)
        if account["role"] != "owner":
            warehouse = account["role"]
        elif warehouse not in ("china", "tajik"):
            warehouse = "china"
        return view(request, "scan.html", warehouse=warehouse)

    @app.post("/api/scans")
    async def scan(request: Request):
        account = identity(request, False)
        check_csrf(request, request.headers.get("x-csrf-token"))
        payload = await request.json()
        if not isinstance(payload, dict) or any(not isinstance(payload.get(k), str) for k in ("warehouse", "track", "request_id")):
            raise ValueError("Неверный запрос")
        result = await run_in_threadpool(store.scan, account["id"], payload["warehouse"], payload["track"], payload["request_id"])
        return result

    @app.get("/admin/parcels")
    def parcels_page(request: Request, q: str = "", page: int = 1):
        account = identity(request, False)
        page = max(1, page)
        parcels = store.list_parcels(warehouse=account["role"], search=q, limit=51, offset=(page-1)*50)
        return view(request, "parcels.html", parcels=parcels[:50], more=len(parcels)>50, page=page, q=q[:40])

    @app.get("/admin/parcels/{parcel_id:int}")
    def parcel_page(request: Request, parcel_id: int):
        account = identity(request, False)
        parcel = store.one("SELECT * FROM parcels WHERE id=? AND deleted=0", (parcel_id,))
        if not parcel:
            raise HTTPException(404, "Посылка не найдена")
        events = store.rows("SELECT e.*,a.username FROM events e LEFT JOIN accounts a ON a.id=e.account_id WHERE e.parcel_id=? ORDER BY e.id DESC LIMIT 100", (parcel_id,))
        return view(request, "parcel.html", parcel=parcel, editable=store.can_edit(account, parcel), events=events)

    @app.post("/admin/parcels/{parcel_id:int}")
    async def edit_parcel(request: Request, parcel_id: int):
        account = identity(request, False)
        data = await form(request)
        await run_in_threadpool(store.edit_parcel, parcel_id, account["id"], str(data.get("weight", "")), str(data.get("price", "")), data.get("paid") == "on", data.get("stage", ""))
        return redirect(f"/admin/parcels/{parcel_id}", "Сабт шуд / Сохранено")

    @app.post("/admin/parcels/{parcel_id:int}/archive")
    async def archive(request: Request, parcel_id: int):
        account = owner(request)
        await form(request)
        store.archive_parcel(parcel_id, account["id"])
        return redirect("/admin/parcels", "Бор бойгонӣ шуд / Посылка архивирована")

    @app.get("/admin/claims")
    def claims_page(request: Request):
        identity(request, False)
        claims = store.rows("SELECT c.*,p.track,p.stage,u.name,u.telegram_id,u.tg_username FROM claims c JOIN parcels p ON p.id=c.parcel_id JOIN people u ON u.id=c.person_id WHERE c.state='pending' AND p.deleted=0 ORDER BY c.id LIMIT 100")
        return view(request, "claims.html", claims=claims)

    @app.post("/admin/claims/{claim_id:int}")
    async def claim_decision(request: Request, claim_id: int):
        account = identity(request, False)
        data = await form(request)
        store.decide_claim(claim_id, account["id"], data.get("decision") == "approve")
        return redirect("/admin/claims", "Дархост коркард шуд / Заявка обработана")

    @app.get("/admin/clients")
    def clients_page(request: Request, page: int = 1):
        identity(request, False)
        page = max(1, page)
        clients = store.rows("SELECT u.*,COUNT(p.id) parcels FROM people u LEFT JOIN parcels p ON p.owner_id=u.id AND p.deleted=0 GROUP BY u.id ORDER BY u.id DESC LIMIT 50 OFFSET ?", ((page-1)*50,))
        return view(request, "clients.html", clients=clients, page=page)

    @app.get("/admin/support")
    def support_page(request: Request):
        identity(request, False)
        tickets = store.rows("SELECT t.*,p.name,p.tg_username FROM tickets t JOIN people p ON p.id=t.person_id ORDER BY CASE WHEN t.state='open' THEN 0 ELSE 1 END,t.id DESC LIMIT 100")
        return view(request, "support.html", tickets=tickets)

    @app.post("/admin/support/{ticket_id:int}")
    async def answer(request: Request, ticket_id: int):
        account = identity(request, False)
        data = await form(request)
        store.answer_ticket(ticket_id, account["id"], str(data.get("text", "")), data.get("action") == "reject")
        return redirect("/admin/support", "Ҷавоб сабт шуд / Ответ сохранён")

    @app.get("/admin/broadcast")
    def broadcast_page(request: Request):
        owner(request)
        return view(request, "broadcast.html", stats=store.stats())

    @app.post("/admin/broadcast")
    async def broadcast(request: Request):
        account = owner(request)
        data = await form(request)
        count = store.broadcast(str(data.get("text", "")), account["id"])
        return redirect("/admin/broadcast", f"Ба навбат гузошта шуд / В очереди: {count}")

    @app.get("/admin/settings")
    def settings_page(request: Request):
        owner(request)
        messages = store.rows("SELECT id,event_key,state,error,attempts,created FROM outbox ORDER BY id DESC LIMIT 30")
        admins = store.rows("SELECT a.id,a.username,a.role,a.enabled,GROUP_CONCAT(t.telegram_id) telegram_ids FROM accounts a LEFT JOIN telegram_admins t ON t.account_id=a.id WHERE a.role<>'customer' GROUP BY a.id")
        return view(request, "settings.html", messages=messages, admins=admins)

    @app.post("/admin/settings")
    async def save_settings(request: Request):
        owner(request)
        data = await form(request)
        store.set_preferences(str(data.get("rate", "")), str(data.get("currency", "")))
        return redirect("/admin/settings", "Танзимот сабт шуд / Настройки сохранены")

    @app.post("/admin/settings/retry")
    async def retry(request: Request):
        account = owner(request)
        await form(request)
        store.retry_messages(account["id"])
        return redirect("/admin/settings", "Такроран ба навбат гузошта шуд / Повторная отправка запланирована")

    @app.get("/admin/export")
    def export(request: Request):
        account = identity(request, False)
        workbook, sheet = Workbook(), None
        sheet = workbook.active
        sheet.title = "Борҳо"
        sheet.append(["Трек", "Ҳолат", "Муштарӣ", "Вазн, кг", "Нарх", "Пардохт", "Сана"])
        def safe(value):
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                return "'"+value
            return value
        offset = 0
        while True:
            rows = store.list_parcels(warehouse=account["role"], limit=1000, offset=offset)
            for p in rows:
                sheet.append([safe(p["track"]), status(p["stage"]), safe(p["owner_name"] or "Бе соҳиб"), p["weight_g"]/1000 if p["weight_g"] else None, p["price_minor"]/100, bool(p["paid"]), datetime.fromtimestamp(p["created"], DUSHANBE_TIMEZONE).isoformat()])
            if len(rows) < 1000:
                break
            offset += 1000
        buffer = BytesIO()
        workbook.save(buffer)
        return Response(buffer.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="tuman-parcels.xlsx"'})

    return app

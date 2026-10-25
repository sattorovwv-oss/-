from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import json
import os
import secrets
import sqlite3
import time
import uuid

from .security import digest, minor, number, password_hash, password_matches, track_code, username
from .texts import card

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
 id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE, name TEXT NOT NULL,
 tg_username TEXT NOT NULL DEFAULT '', lang TEXT, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('customer','china','tajik','owner')),
 person_id INTEGER UNIQUE REFERENCES people(id), enabled INTEGER NOT NULL DEFAULT 1,
 created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_admins (
 telegram_id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id)
);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, account_id INTEGER REFERENCES accounts(id), csrf TEXT NOT NULL,
 created INTEGER NOT NULL, seen INTEGER NOT NULL, expires INTEGER NOT NULL,
 agent TEXT NOT NULL, ip TEXT NOT NULL, method TEXT NOT NULL DEFAULT 'password'
);
CREATE TABLE IF NOT EXISTS login_tokens (
 id TEXT PRIMARY KEY, account_id INTEGER REFERENCES accounts(id),
 person_id INTEGER REFERENCES people(id), expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (key TEXT NOT NULL, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_key ON attempts(key,at);
CREATE TABLE IF NOT EXISTS external_identities (
 provider TEXT NOT NULL CHECK(provider='google'), subject TEXT NOT NULL,
 account_id INTEGER NOT NULL REFERENCES accounts(id), email TEXT NOT NULL,
 created INTEGER NOT NULL, updated INTEGER NOT NULL,
 PRIMARY KEY(provider,subject), UNIQUE(provider,account_id)
);
CREATE TABLE IF NOT EXISTS oauth_requests (
 state_hash TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
 verifier TEXT NOT NULL, expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS parcels (
 id INTEGER PRIMARY KEY, track TEXT NOT NULL UNIQUE, owner_id INTEGER REFERENCES people(id),
 stage INTEGER NOT NULL DEFAULT 0 CHECK(stage IN (0,10,20,30,40)), weight_g INTEGER,
 price_minor INTEGER NOT NULL DEFAULT 0, paid INTEGER NOT NULL DEFAULT 0,
 deleted INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, updated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS parcel_owner ON parcels(owner_id,deleted,id);
CREATE TABLE IF NOT EXISTS claims (
 id INTEGER PRIMARY KEY, parcel_id INTEGER NOT NULL REFERENCES parcels(id),
 person_id INTEGER NOT NULL REFERENCES people(id), state TEXT NOT NULL DEFAULT 'pending',
 created INTEGER NOT NULL, UNIQUE(parcel_id,person_id)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, parcel_id INTEGER REFERENCES parcels(id),
 account_id INTEGER REFERENCES accounts(id), action TEXT NOT NULL,
 data TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_requests (
 id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
 track TEXT NOT NULL, warehouse TEXT NOT NULL, result TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
 id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, chat_id INTEGER NOT NULL,
 parcel_id INTEGER REFERENCES parcels(id),
 text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_at INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created INTEGER NOT NULL,
 sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state,next_at);
CREATE TABLE IF NOT EXISTS tickets (
 id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id),
 text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open', answer TEXT NOT NULL DEFAULT '',
 account_id INTEGER REFERENCES accounts(id), created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO settings VALUES ('rate_minor','0');
INSERT OR IGNORE INTO settings VALUES ('currency','сомонӣ');
PRAGMA user_version=2;
"""


class ConfirmTelegramLink(ValueError):
    def __init__(self, person):
        self.name = person["name"]
        self.telegram_id = person["telegram_id"]
        super().__init__("Подтвердите привязку собственного Telegram")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        # SQLite inherits this mode for the WAL; set it before first connecting.
        self.path.chmod(0o600)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        self.path.chmod(0o600)
        self.dummy_hash = password_hash(secrets.token_urlsafe(24))

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connection() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def bootstrap(self, settings):
        for role in ("owner", "china", "tajik"):
            # Find the initial account by its role, not mutable login. A login change
            # must never recreate an old account from bootstrap environment values.
            account = self.one("SELECT * FROM accounts WHERE role=? ORDER BY id LIMIT 1", (role,))
            password = settings.passwords.get(role)
            if not account and password:
                account = self.create_account(role, password, role=role)
            ids = settings.telegram_ids.get(role, [])
            if ids and not account:
                raise ValueError(f"Create the {role} account before binding Telegram IDs")
            for telegram_id in ids:
                with self.connection(True) as db:
                    existing = db.execute("SELECT account_id FROM telegram_admins WHERE telegram_id=?", (telegram_id,)).fetchone()
                    if existing and existing[0] != account["id"]:
                        raise ValueError("A Telegram ID is configured for multiple admin accounts")
                    db.execute("INSERT OR IGNORE INTO telegram_admins VALUES (?,?)", (telegram_id, account["id"]))

    def create_account(self, login, password, name="", role="customer"):
        login, hashed = username(login), password_hash(password)
        if role not in ("customer", "owner", "china", "tajik"):
            raise ValueError("Unknown role")
        if len(name) > 100:
            raise ValueError("Имя слишком длинное")
        try:
            with self.connection(True) as db:
                person_id = None
                if role == "customer":
                    person_id = db.execute("INSERT INTO people(name,lang,created) VALUES (?,'ru',?)", (name.strip() or login, int(time.time()))).lastrowid
                aid = db.execute("INSERT INTO accounts(username,password_hash,role,person_id,created) VALUES (?,?,?,?,?)", (login, hashed, role, person_id, int(time.time()))).lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("Этот логин уже занят / Ин логин банд аст") from None
        return self.account(aid)

    def account(self, account_id):
        return self.one("SELECT * FROM accounts WHERE id=? AND enabled=1", (account_id,))

    def admin_for_telegram(self, telegram_id):
        return self.one("SELECT a.* FROM telegram_admins t JOIN accounts a ON a.id=t.account_id WHERE t.telegram_id=? AND a.enabled=1", (telegram_id,))

    def limited(self, key, limit=8, seconds=900):
        now = int(time.time())
        with self.connection(True) as db:
            db.execute("DELETE FROM attempts WHERE at<?", (now - 86400,))
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE key=? AND at>?", (key, now-seconds)).fetchone()[0]
            if count >= limit:
                return True
            db.execute("INSERT INTO attempts VALUES (?,?)", (key, now))
        return False

    def authenticate(self, login, password, ip):
        if self.limited("login:"+digest(ip), limit=20) or self.limited("login-user:"+digest(login.lower()), limit=10):
            raise ValueError("Слишком много попыток. Повторите через 15 минут.")
        account = self.one("SELECT * FROM accounts WHERE username=? AND enabled=1", (login.strip().lower(),))
        ok = password_matches(password, account["password_hash"] if account else self.dummy_hash)
        if not account or not ok:
            raise ValueError("Неверный логин или пароль / Логин ё рамз нодуруст аст")
        return account

    def new_session(self, account_id, agent="", ip="", method="password"):
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), int(time.time())
        with self.connection(True) as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (now,))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", (digest(token), account_id, csrf, now, now, now+43200, agent[:300], ip[:64], method))
        return token

    def session(self, token):
        if not token or len(token) > 100:
            return None
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (digest(token), int(time.time()))).fetchone()
            if row:
                db.execute("UPDATE sessions SET seen=? WHERE id=?", (int(time.time()), row["id"]))
        return dict(row) if row else None

    def revoke_session(self, session_id, account_id):
        with self.connection(True) as db:
            db.execute("DELETE FROM sessions WHERE id=? AND account_id=?", (session_id, account_id))

    def change_credentials(self, account_id, current_password, login, new_password, keep_session):
        account = self.account(account_id)
        if not account:
            raise ValueError("Текущий пароль неверен / Рамзи ҷорӣ нодуруст аст")
        if account["password_hash"]:
            if not password_matches(current_password, account["password_hash"]):
                raise ValueError("Текущий пароль неверен / Рамзи ҷорӣ нодуруст аст")
        else:
            now = int(time.time())
            fresh_google = self.one("SELECT s.id FROM sessions s JOIN external_identities e ON e.account_id=s.account_id AND e.provider='google' WHERE s.id=? AND s.account_id=? AND s.method='google' AND s.created>=? AND s.expires>?", (keep_session, account_id, now-600, now))
            if account["role"] != "customer" or not fresh_google:
                raise ValueError("Для установки первого пароля заново войдите через Google и откройте профиль в течение 10 минут.")
            if not new_password:
                raise ValueError("Укажите новый пароль: от 12 до 128 символов.")
        login = username(login)
        hashed = password_hash(new_password) if new_password else account["password_hash"]
        try:
            with self.connection(True) as db:
                db.execute("UPDATE accounts SET username=?,password_hash=? WHERE id=?", (login, hashed, account_id))
                db.execute("DELETE FROM sessions WHERE account_id=? AND id<>?", (account_id, keep_session))
                db.execute("DELETE FROM login_tokens WHERE account_id=?", (account_id,))
                db.execute("INSERT INTO events(account_id,action,data,created) VALUES (?,'credentials_changed','{}',?)", (account_id, int(time.time())))
        except sqlite3.IntegrityError:
            raise ValueError("Этот логин занят") from None

    def begin_google_auth(self, session_id, account_id=None):
        state, verifier, now = secrets.token_urlsafe(32), secrets.token_urlsafe(48), int(time.time())
        with self.connection(True) as db:
            session = db.execute("SELECT account_id FROM sessions WHERE id=? AND expires>?", (session_id, now)).fetchone()
            if not session or session["account_id"] != account_id:
                raise ValueError("Обновите страницу и повторите вход через Google.")
            if account_id is not None:
                account = db.execute("SELECT role FROM accounts WHERE id=? AND enabled=1", (account_id,)).fetchone()
                if not account or account["role"] != "customer":
                    raise PermissionError("Вход через Google доступен клиентам.")
            db.execute("DELETE FROM oauth_requests WHERE expires<=?", (now,))
            db.execute("INSERT INTO oauth_requests VALUES (?,?,?,?,?)", (digest(state), session_id, account_id, verifier, now+600))
        return state, verifier

    def consume_google_auth(self, state, session_id):
        if not isinstance(state, str) or not 20 <= len(state) <= 100 or not session_id:
            raise ValueError("Ссылка входа устарела. Начните вход через Google заново.")
        with self.connection(True) as db:
            row = db.execute("SELECT o.* FROM oauth_requests o JOIN sessions s ON s.id=o.session_id WHERE o.state_hash=? AND o.session_id=? AND o.expires>? AND s.expires>? AND s.account_id IS o.account_id", (digest(state), session_id, int(time.time()), int(time.time()))).fetchone()
            if not row:
                raise ValueError("Ссылка входа устарела. Начните вход через Google заново.")
            db.execute("DELETE FROM oauth_requests WHERE state_hash=?", (row["state_hash"],))
        return dict(row)

    def complete_google_auth(self, flow, profile, agent="", ip=""):
        # This accepts only a server-validated UserInfo result, never browser JSON.
        from .google_auth import verified_profile
        profile = verified_profile(profile)
        token, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), int(time.time())
        with self.connection(True) as db:
            previous = db.execute("SELECT account_id FROM sessions WHERE id=? AND expires>?", (flow["session_id"], now)).fetchone()
            if not previous or previous["account_id"] != flow["account_id"]:
                raise ValueError("Сеанс изменился. Начните вход через Google заново.")
            linked = db.execute("SELECT account_id FROM external_identities WHERE provider='google' AND subject=?", (profile["sub"],)).fetchone()
            if flow["account_id"] is not None:
                aid = flow["account_id"]
                if linked and linked["account_id"] != aid:
                    raise ValueError("Этот Google уже связан с другим аккаунтом. Выйдите из текущего аккаунта и войдите через Google.")
                other = db.execute("SELECT subject FROM external_identities WHERE provider='google' AND account_id=?", (aid,)).fetchone()
                if other and other["subject"] != profile["sub"]:
                    raise ValueError("К аккаунту уже подключён другой Google.")
            elif linked:
                aid = linked["account_id"]
            else:
                login = "google_" + secrets.token_hex(12)
                pid = db.execute("INSERT INTO people(name,lang,created) VALUES (?,'ru',?)", (profile["name"], now)).lastrowid
                aid = db.execute("INSERT INTO accounts(username,password_hash,role,person_id,created) VALUES (?,'','customer',?,?)", (login, pid, now)).lastrowid
            account = db.execute("SELECT * FROM accounts WHERE id=? AND enabled=1 AND role='customer'", (aid,)).fetchone()
            if not account:
                raise ValueError("Вход через Google для этого аккаунта недоступен.")
            db.execute("INSERT INTO external_identities VALUES ('google',?,?,?,?,?) ON CONFLICT(provider,subject) DO UPDATE SET email=excluded.email,updated=excluded.updated", (profile["sub"], aid, profile["email"], now, now))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?)", (digest(token), aid, csrf, now, now, now+43200, agent[:300], ip[:64], "google"))
            db.execute("DELETE FROM sessions WHERE id=?", (flow["session_id"],))
        return self.account(aid), token

    def issue_link(self, person_id=None, account_id=None):
        if (person_id is None) == (account_id is None):
            raise ValueError("Exactly one identity is required")
        token = secrets.token_urlsafe(32)
        with self.connection(True) as db:
            db.execute("DELETE FROM login_tokens WHERE expires<? OR used=1", (int(time.time()),))
            db.execute("INSERT INTO login_tokens VALUES (?,?,?,?,0)", (digest(token), account_id, person_id, int(time.time())+120))
        return token

    def exchange_link(self, token, current_account_id=None, confirm_link=False):
        if len(token) > 100:
            raise ValueError("Ссылка недействительна")
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM login_tokens WHERE id=? AND used=0 AND expires>?", (digest(token), int(time.time()))).fetchone()
            if not row:
                raise ValueError("Ссылка истекла или уже использована. Получите новую в боте.")
            aid = row["account_id"]
            if aid is None:
                existing = db.execute("SELECT * FROM accounts WHERE person_id=? AND enabled=1", (row["person_id"],)).fetchone()
                if existing:
                    # A previously linked Telegram identity logs into its own account only.
                    if current_account_id and current_account_id != existing["id"]:
                        raise ValueError("Этот Telegram уже связан с другим аккаунтом")
                    aid = existing["id"]
                else:
                    account = db.execute("SELECT * FROM accounts WHERE id=? AND role='customer' AND enabled=1", (current_account_id,)).fetchone()
                    if not account:
                        raise ValueError("Сначала зарегистрируйтесь или войдите в клиентский аккаунт, затем откройте новую ссылку из бота.")
                    current_person = db.execute("SELECT * FROM people WHERE id=?", (account["person_id"],)).fetchone()
                    if current_person["telegram_id"] is not None:
                        raise ValueError("К аккаунту уже привязан Telegram. Обратитесь в поддержку для смены.")
                    src, dst = row["person_id"], account["person_id"]
                    source = db.execute("SELECT * FROM people WHERE id=?", (src,)).fetchone()
                    if not confirm_link:
                        raise ConfirmTelegramLink(source)
                    db.execute("UPDATE parcels SET owner_id=? WHERE owner_id=?", (dst, src))
                    # Merge duplicate pending requests before moving remaining ones.
                    db.execute("DELETE FROM claims WHERE person_id=? AND parcel_id IN (SELECT parcel_id FROM claims WHERE person_id=?)", (dst, src))
                    db.execute("UPDATE claims SET person_id=? WHERE person_id=?", (dst, src))
                    db.execute("UPDATE tickets SET person_id=? WHERE person_id=?", (dst, src))
                    db.execute("DELETE FROM login_tokens WHERE person_id=? AND id<>?", (src, digest(token)))
                    db.execute("UPDATE login_tokens SET person_id=? WHERE id=?", (dst, digest(token)))
                    db.execute("DELETE FROM people WHERE id=?", (src,))
                    db.execute("UPDATE people SET telegram_id=?,tg_username=?,lang=? WHERE id=?", (source["telegram_id"], source["tg_username"], source["lang"] or "ru", dst))
                    aid = account["id"]
            account = db.execute("SELECT * FROM accounts WHERE id=? AND enabled=1", (aid,)).fetchone()
            if not account:
                raise ValueError("Учётная запись отключена")
            db.execute("UPDATE login_tokens SET used=1 WHERE id=?", (digest(token),))
        return self.account(aid)

    def telegram_person(self, chat_id, name, tg_username="", language=None):
        with self.connection(True) as db:
            db.execute("INSERT INTO people(telegram_id,name,tg_username,lang,created) VALUES (?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name,tg_username=excluded.tg_username", (chat_id, name[:100], tg_username[:100], language, int(time.time())))
            if language:
                db.execute("UPDATE people SET lang=? WHERE telegram_id=?", (language, chat_id))
        return self.one("SELECT * FROM people WHERE telegram_id=?", (chat_id,))

    def preferences(self):
        return {r["key"]: r["value"] for r in self.rows("SELECT * FROM settings")}

    def set_preferences(self, rate, currency):
        value = minor(rate)
        currency = currency.strip()
        if not currency or len(currency) > 12:
            raise ValueError("Валюта: 1–12 символов")
        with self.connection(True) as db:
            db.execute("UPDATE settings SET value=? WHERE key='rate_minor'", (str(value),))
            db.execute("UPDATE settings SET value=? WHERE key='currency'", (currency,))

    def parcel(self, track):
        return self.one("SELECT * FROM parcels WHERE track=? AND deleted=0", (track_code(track),))

    def add_track(self, person_id, track):
        track, now = track_code(track), int(time.time())
        with self.connection(True) as db:
            db.execute("INSERT OR IGNORE INTO parcels(track,created,updated) VALUES (?,?,?)", (track, now, now))
            parcel = db.execute("SELECT * FROM parcels WHERE track=?", (track,)).fetchone()
            if parcel["deleted"]:
                raise ValueError("Запись архивирована. Обратитесь в поддержку.")
            if parcel["owner_id"]:
                return "duplicate" if parcel["owner_id"] == person_id else "taken"
            db.execute("INSERT INTO claims(parcel_id,person_id,created) VALUES (?,?,?) ON CONFLICT(parcel_id,person_id) DO UPDATE SET state=CASE WHEN claims.state='rejected' THEN 'pending' ELSE claims.state END", (parcel["id"], person_id, now))
        return "pending"

    def _notify(self, db, parcel, key, heading=None):
        if not parcel["owner_id"]:
            return
        person = db.execute("SELECT * FROM people WHERE id=?", (parcel["owner_id"],)).fetchone()
        if not person or not person["telegram_id"]:
            return
        currency = db.execute("SELECT value FROM settings WHERE key='currency'").fetchone()[0]
        text = card(dict(parcel), person["lang"] or "tj", currency=currency)
        if heading:
            text = heading[0 if person["lang"] == "ru" else 1] + "\n\n" + text
        db.execute("INSERT OR IGNORE INTO outbox(event_key,chat_id,parcel_id,text,created) VALUES (?,?,?,?,?)", (key, person["telegram_id"], parcel["id"], text, int(time.time())))

    def decide_claim(self, claim_id, admin_id, approve=True):
        account = self.account(admin_id)
        if not account or account["role"] == "customer":
            raise PermissionError("Нет доступа к подтверждению владельца")
        with self.connection(True) as db:
            claim = db.execute("SELECT * FROM claims WHERE id=? AND state='pending'", (claim_id,)).fetchone()
            if not claim:
                raise ValueError("Заявка уже обработана")
            parcel = db.execute("SELECT * FROM parcels WHERE id=? AND deleted=0", (claim["parcel_id"],)).fetchone()
            if not parcel:
                raise ValueError("Посылка не найдена")
            if approve:
                if parcel["owner_id"] and parcel["owner_id"] != claim["person_id"]:
                    raise ValueError("Владелец уже назначен")
                db.execute("UPDATE parcels SET owner_id=?,updated=? WHERE id=?", (claim["person_id"], int(time.time()), parcel["id"]))
                db.execute("UPDATE claims SET state='rejected' WHERE parcel_id=? AND state='pending'", (parcel["id"],))
                db.execute("UPDATE claims SET state='approved' WHERE id=?", (claim_id,))
                parcel = db.execute("SELECT * FROM parcels WHERE id=?", (parcel["id"],)).fetchone()
                self._notify(db, parcel, f"claim:{claim_id}", ("✅ Посылка привязана к вашему аккаунту", "✅ Бор ба ҳисоби шумо пайваст шуд"))
            else:
                db.execute("UPDATE claims SET state='rejected' WHERE id=?", (claim_id,))
            db.execute("INSERT INTO events(parcel_id,account_id,action,data,created) VALUES (?,?,?, ?,?)", (parcel["id"], admin_id, "claim_approved" if approve else "claim_rejected", json.dumps({"claim_id": claim_id}), int(time.time())))

    def scan(self, admin_id, warehouse, track, request_id):
        track = track_code(track)
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Неверный идентификатор сканирования") from None
        account = self.account(admin_id)
        if not account or warehouse not in ("china", "tajik") or account["role"] not in (warehouse, "owner"):
            raise PermissionError("Сканирование чужого склада запрещено")
        target, now = (10 if warehouse == "china" else 30), int(time.time())
        with self.connection(True) as db:
            previous = db.execute("SELECT * FROM scan_requests WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["account_id"] != admin_id or previous["track"] != track or previous["warehouse"] != warehouse:
                    raise ValueError("Идентификатор уже использован для другого скана")
                return json.loads(previous["result"])
            db.execute("INSERT OR IGNORE INTO parcels(track,created,updated) VALUES (?,?,?)", (track, now, now))
            parcel = db.execute("SELECT * FROM parcels WHERE track=?", (track,)).fetchone()
            if parcel["deleted"]:
                raise ValueError("Посылка архивирована. Восстановление выполняет владелец.")
            outcome = "accepted" if parcel["stage"] < target else "duplicate" if parcel["stage"] == target else "ahead"
            if outcome == "accepted":
                db.execute("UPDATE parcels SET stage=?,updated=? WHERE id=?", (target, now, parcel["id"]))
                parcel = db.execute("SELECT * FROM parcels WHERE id=?", (parcel["id"],)).fetchone()
                db.execute("INSERT INTO events(parcel_id,account_id,action,data,created) VALUES (?,?,'scan',?,?)", (parcel["id"], admin_id, json.dumps({"warehouse": warehouse, "stage": target}), now))
                self._notify(db, parcel, f"stage:{parcel['id']}:{target}")
            notification = db.execute("SELECT state FROM outbox WHERE event_key=?", (f"stage:{parcel['id']}:{target}",)).fetchone()
            result = {"track": track, "parcel_id": parcel["id"], "stage": parcel["stage"], "outcome": outcome, "notification": notification[0] if notification else "no_telegram_owner"}
            db.execute("INSERT INTO scan_requests VALUES (?,?,?,?,?,?)", (request_id, admin_id, track, warehouse, json.dumps(result), now))
        return result

    @staticmethod
    def can_edit(account, parcel):
        return account["role"] == "owner" or (account["role"] == "china" and parcel["stage"] in (0, 10, 20)) or (account["role"] == "tajik" and parcel["stage"] in (30, 40))

    def edit_parcel(self, parcel_id, admin_id, weight, price, paid, stage):
        account = self.account(admin_id)
        grams = int((number(weight) * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if str(weight).strip() else None
        price_value = minor(price) if str(price).strip() else None
        stage = int(stage)
        with self.connection(True) as db:
            old = db.execute("SELECT * FROM parcels WHERE id=? AND deleted=0", (parcel_id,)).fetchone()
            if not old:
                raise ValueError("Посылка не найдена")
            if not account or not self.can_edit(account, old):
                raise PermissionError("Нет прав менять эту посылку")
            if stage != old["stage"]:
                allowed = {"china": {(10, 20)}, "tajik": {(30, 40)}, "owner": {(10, 20), (30, 40)}}
                if (old["stage"], stage) not in allowed.get(account["role"], set()):
                    raise ValueError("Переход запрещён. Приём на склад выполняется сканированием.")
            if price_value is None:
                rate = int(db.execute("SELECT value FROM settings WHERE key='rate_minor'").fetchone()[0])
                price_value = int((Decimal(grams or 0) * rate / 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            db.execute("UPDATE parcels SET weight_g=?,price_minor=?,paid=?,stage=?,updated=? WHERE id=?", (grams, price_value, int(bool(paid)), stage, int(time.time()), parcel_id))
            parcel = db.execute("SELECT * FROM parcels WHERE id=?", (parcel_id,)).fetchone()
            db.execute("INSERT INTO events(parcel_id,account_id,action,data,created) VALUES (?,?,'edit',?,?)", (parcel_id, admin_id, json.dumps({"weight_g": grams, "price_minor": price_value, "paid": bool(paid), "stage": stage}), int(time.time())))
            if stage != old["stage"]:
                self._notify(db, parcel, f"stage:{parcel_id}:{stage}")
            elif any(parcel[k] != old[k] for k in ("weight_g", "price_minor", "paid")):
                self._notify(db, parcel, "details:"+str(uuid.uuid4()), ("Обновлены данные вашей посылки", "Маълумоти бори шумо нав шуд"))

    def archive_parcel(self, parcel_id, admin_id):
        account = self.account(admin_id)
        if not account or account["role"] != "owner":
            raise PermissionError("Только владелец может архивировать посылки")
        with self.connection(True) as db:
            db.execute("UPDATE parcels SET deleted=1,updated=? WHERE id=?", (int(time.time()), parcel_id))
            db.execute("INSERT INTO events(parcel_id,account_id,action,data,created) VALUES (?,?,'archive','{}',?)", (parcel_id, admin_id, int(time.time())))

    def list_parcels(self, person_id=None, warehouse=None, search="", limit=100, offset=0):
        clauses, args = ["p.deleted=0"], []
        if person_id is not None:
            clauses.append("p.owner_id=?")
            args.append(person_id)
        if warehouse == "china":
            clauses.append("p.stage IN (0,10,20)")
        elif warehouse == "tajik":
            clauses.append("p.stage IN (30,40)")
        if search:
            clauses.append("p.track LIKE ?")
            args.append("%"+"".join(search.split()).upper()[:40]+"%")
        sql = "SELECT p.*,u.name AS owner_name FROM parcels p LEFT JOIN people u ON u.id=p.owner_id WHERE " + " AND ".join(clauses) + " ORDER BY p.id DESC LIMIT ? OFFSET ?"
        return self.rows(sql, (*args, min(limit, 10000), max(0, offset)))

    def create_ticket(self, person_id, text):
        text = text.strip()
        if not text or len(text) > 3000:
            raise ValueError("Сообщение: 1–3000 символов")
        from html import escape
        with self.connection(True) as db:
            ticket_id = db.execute("INSERT INTO tickets(person_id,text,created) VALUES (?,?,?)", (person_id, text, int(time.time()))).lastrowid
            person = db.execute("SELECT name FROM people WHERE id=?", (person_id,)).fetchone()
            for admin in db.execute("SELECT t.telegram_id FROM telegram_admins t JOIN accounts a ON a.id=t.account_id WHERE a.enabled=1").fetchall():
                message = f"💬 Паёми нав №{ticket_id}\n👤 {escape(person['name'])}\n\n{escape(text)}"
                db.execute("INSERT INTO outbox(event_key,chat_id,text,created) VALUES (?,?,?,?)", (f"ticket_notice:{ticket_id}:{admin[0]}", admin[0], message, int(time.time())))
            return ticket_id

    def answer_ticket(self, ticket_id, admin_id, text, reject=False):
        if not self.account(admin_id) or self.account(admin_id)["role"] == "customer":
            raise PermissionError("Нет доступа")
        if not reject and (not text.strip() or len(text) > 3000):
            raise ValueError("Ответ: 1–3000 символов")
        from html import escape
        with self.connection(True) as db:
            ticket = db.execute("SELECT t.*,p.telegram_id,p.lang FROM tickets t JOIN people p ON p.id=t.person_id WHERE t.id=? AND t.state='open'", (ticket_id,)).fetchone()
            if not ticket:
                raise ValueError("Обращение уже обработано")
            db.execute("UPDATE tickets SET answer=?,state=?,account_id=? WHERE id=?", (text.strip(), "rejected" if reject else "answered", admin_id, ticket_id))
            if ticket["telegram_id"]:
                message = ("Обращение отклонено. / Дархост рад шуд." if reject else "💬 Ответ / Ҷавоб:\n\n" + escape(text.strip()))
                db.execute("INSERT INTO outbox(event_key,chat_id,text,created) VALUES (?,?,?,?)", (f"ticket:{ticket_id}", ticket["telegram_id"], message, int(time.time())))

    def broadcast(self, text, admin_id):
        account = self.account(admin_id)
        if not account or account["role"] != "owner":
            raise PermissionError("Рассылка доступна владельцу")
        if not text.strip() or len(text) > 3000:
            raise ValueError("Рассылка: 1–3000 символов")
        from html import escape
        batch = str(uuid.uuid4())
        with self.connection(True) as db:
            people = db.execute("SELECT telegram_id FROM people WHERE telegram_id IS NOT NULL").fetchall()
            db.executemany("INSERT INTO outbox(event_key,chat_id,text,created) VALUES (?,?,?,?)", [(f"broadcast:{batch}:{p[0]}", p[0], escape(text.strip()), int(time.time())) for p in people])
            db.execute("INSERT INTO events(account_id,action,data,created) VALUES (?,'broadcast',?,?)", (admin_id, json.dumps({"batch": batch, "recipients": len(people)}), int(time.time())))
        return len(people)

    def stats(self):
        counts = {r["stage"]: r["n"] for r in self.rows("SELECT stage,COUNT(*) n FROM parcels WHERE deleted=0 GROUP BY stage")}
        return {"total": sum(counts.values()), "china": counts.get(10, 0), "transit": counts.get(20, 0), "tajik": counts.get(30, 0), "delivered": counts.get(40, 0), "claims": self.one("SELECT COUNT(*) n FROM claims WHERE state='pending'")["n"], "tickets": self.one("SELECT COUNT(*) n FROM tickets WHERE state='open'")["n"], "queue": self.one("SELECT COUNT(*) n FROM outbox WHERE state IN ('pending','blocked')")["n"], "customers": self.one("SELECT COUNT(*) n FROM people")["n"]}

    def due_messages(self, limit=20):
        return self.rows("SELECT * FROM outbox WHERE state='pending' AND next_at<=? ORDER BY id LIMIT ?", (int(time.time()), limit))

    def queue_connection_notice(self, chat_id, update_id, text):
        """One recovery notice per failed Telegram update, persisted for retry."""
        with self.connection(True) as db:
            db.execute("INSERT OR IGNORE INTO outbox(event_key,chat_id,text,created) VALUES (?,?,?,?)", (f"connection:{chat_id}:{update_id}", chat_id, text, int(time.time())))

    def prepare_message(self, message):
        """Suppress obsolete stage alerts and refresh other parcel cards at send time."""
        if not message["parcel_id"]:
            return message["text"]
        parcel = self.one("SELECT * FROM parcels WHERE id=? AND deleted=0", (message["parcel_id"],))
        obsolete = not parcel
        if message["event_key"].startswith("stage:") and parcel:
            obsolete = parcel["stage"] > int(message["event_key"].split(":")[-1])
        if obsolete:
            with self.connection(True) as db:
                db.execute("UPDATE outbox SET state='superseded',error='Newer parcel state exists' WHERE id=?", (message["id"],))
            return None
        person = self.one("SELECT * FROM people WHERE id=?", (parcel["owner_id"],))
        return card(parcel, person["lang"] or "tj", currency=self.preferences()["currency"])

    def finish_message(self, message_id, error="", permanent=False, retry_after=None):
        now = int(time.time())
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM outbox WHERE id=?", (message_id,)).fetchone()
            if not row:
                return
            wait = retry_after if retry_after is not None else min(3600, 2 ** min(row["attempts"]+1, 11))
            db.execute("UPDATE outbox SET state=?,attempts=attempts+1,error=?,next_at=?,sent_at=? WHERE id=?", ("blocked" if permanent else "pending" if error else "sent", error[:200], now+int(wait), None if error else now, message_id))

    def retry_messages(self, admin_id):
        if self.account(admin_id)["role"] != "owner":
            raise PermissionError("Нет доступа")
        with self.connection(True) as db:
            db.execute("UPDATE outbox SET state='pending',next_at=0 WHERE state='blocked'")

    def backup(self, folder=None):
        folder = Path(folder) if folder else self.path.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("tuman-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3) + ".db"
        target = folder / name
        temporary = target.with_suffix(".tmp")
        with self.connection() as source:
            dest = sqlite3.connect(temporary)
            try:
                source.backup(dest)
                if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
            finally:
                dest.close()
        temporary.chmod(0o600)
        temporary.replace(target)
        for old in sorted(folder.glob("tuman-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)[28:]:
            old.unlink()
        return target

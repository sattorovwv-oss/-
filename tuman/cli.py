import argparse
import getpass

from .config import Settings
from .db import Store
from .security import password_hash


def main():
    parser = argparse.ArgumentParser(description="Tuman Kargo server administration. Passwords are entered privately.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-admin")
    create.add_argument("--role", choices=["owner", "china", "tajik"], required=True)
    create.add_argument("--login", required=True)
    bind = sub.add_parser("bind-telegram")
    bind.add_argument("--login", required=True)
    bind.add_argument("--id", type=int, required=True)
    reset = sub.add_parser("reset-password")
    reset.add_argument("--login", required=True)
    sub.add_parser("backup")
    sub.add_parser("check-db")
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(settings.db_path)
    if args.command in ("create-admin", "reset-password"):
        password = getpass.getpass("New password (12+ characters): ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match")
        if args.command == "create-admin":
            store.create_account(args.login, password, role=args.role)
        else:
            hashed = password_hash(password)
            with store.connection(True) as db:
                row = db.execute("SELECT id FROM accounts WHERE username=?", (args.login.lower(),)).fetchone()
                if not row:
                    raise SystemExit("Account not found")
                db.execute("UPDATE accounts SET password_hash=? WHERE id=?", (hashed, row[0]))
                db.execute("DELETE FROM sessions WHERE account_id=?", (row[0],))
                db.execute("DELETE FROM login_tokens WHERE account_id=?", (row[0],))
        print("Account updated. Password was not printed or stored in plaintext.")
    elif args.command == "bind-telegram":
        account = store.one("SELECT * FROM accounts WHERE username=? AND role<>'customer' AND enabled=1", (args.login.lower(),))
        if not account:
            raise SystemExit("Admin account not found")
        with store.connection(True) as db:
            existing = db.execute("SELECT account_id FROM telegram_admins WHERE telegram_id=?", (args.id,)).fetchone()
            if existing and existing[0] != account["id"]:
                raise SystemExit("This ID already belongs to another admin; remove its binding explicitly before reassignment")
            db.execute("INSERT OR IGNORE INTO telegram_admins VALUES (?,?)", (args.id, account["id"]))
        print("Telegram identity bound.")
    elif args.command == "backup":
        print(store.backup())
    else:
        with store.connection() as db:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(result)
        print("Database integrity: ok")


if __name__ == "__main__":
    main()

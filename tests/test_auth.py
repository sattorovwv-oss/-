import time
import pytest

from tuman.security import digest
from .conftest import TEST_PASSWORD


def test_admin_link_single_use_expires_and_is_hashed(store):
    aid=store.one("SELECT id FROM accounts WHERE role='china'")["id"]
    token=store.issue_link(account_id=aid)
    assert store.one("SELECT id FROM login_tokens")["id"]==digest(token)
    assert store.exchange_link(token)["id"]==aid
    with pytest.raises(ValueError): store.exchange_link(token)
    expired=store.issue_link(account_id=aid)
    with store.connection(True) as db: db.execute("UPDATE login_tokens SET expires=0")
    with pytest.raises(ValueError): store.exchange_link(expired)


def test_disabled_admin_link_cannot_login(store):
    aid=store.one("SELECT id FROM accounts WHERE role='china'")["id"]
    token=store.issue_link(account_id=aid)
    with store.connection(True) as db: db.execute("UPDATE accounts SET enabled=0 WHERE id=?",(aid,))
    with pytest.raises(ValueError): store.exchange_link(token)


def test_telegram_link_requires_customer_account_then_merges_records(store,customer):
    p=store.telegram_person(202,"TG User",language="tj")
    store.add_track(p["id"],"TRACK123")
    store.create_ticket(p["id"],"Question")
    token=store.issue_link(person_id=p["id"])
    with pytest.raises(ValueError): store.exchange_link(token)
    with pytest.raises(ValueError,match="Подтвердите"):
        store.exchange_link(token,customer["id"])
    result=store.exchange_link(token,customer["id"],True)
    assert result["id"]==customer["id"]
    person=store.one("SELECT * FROM people WHERE telegram_id=202")
    assert person["id"]==customer["person_id"]
    assert store.one("SELECT person_id FROM claims")["person_id"]==person["id"]
    assert store.one("SELECT person_id FROM tickets")["person_id"]==person["id"]
    assert store.exchange_link(store.issue_link(person_id=person["id"]))["id"]==customer["id"]


def test_link_cannot_take_over_an_existing_customer(store,customer):
    p=store.telegram_person(202,"TG")
    store.exchange_link(store.issue_link(person_id=p["id"]),customer["id"],True)
    other=store.create_account("other",TEST_PASSWORD)
    p=store.one("SELECT * FROM people WHERE telegram_id=202")
    with pytest.raises(ValueError): store.exchange_link(store.issue_link(person_id=p["id"]),other["id"])


def test_password_change_ends_other_sessions_and_magic_links(store,customer):
    keep=store.new_session(customer["id"])
    other=store.new_session(customer["id"])
    link=store.issue_link(account_id=customer["id"])
    store.change_credentials(customer["id"],TEST_PASSWORD,"renamed","New-test-password-9374",digest(keep))
    assert store.session(keep)
    assert store.session(other) is None
    with pytest.raises(ValueError): store.exchange_link(link)
    assert store.authenticate("renamed","New-test-password-9374","127.0.0.1")
    with pytest.raises(ValueError): store.authenticate("customer",TEST_PASSWORD,"127.0.0.1")


def test_sessions_are_isolated(store,customer):
    owner=store.one("SELECT id FROM accounts WHERE role='owner'")["id"]
    token=store.new_session(owner)
    store.revoke_session(digest(token),customer["id"])
    assert store.session(token)
    with store.connection(True) as db: db.execute("UPDATE sessions SET expires=0")
    assert store.session(token) is None


def test_password_change_rejects_wrong_current_password(store,customer):
    with pytest.raises(ValueError): store.change_credentials(customer["id"],"wrong","renamed","New-password-1234","none")
    assert store.account(customer["id"])["username"]=="customer"


def test_login_rate_limit_applies_to_valid_password_too(store):
    for _ in range(10):
        with pytest.raises(ValueError): store.authenticate("china","wrong","127.0.0.1")
    with pytest.raises(ValueError,match="попыток"): store.authenticate("china",TEST_PASSWORD,"127.0.0.1")


def test_bootstrap_does_not_recreate_account_after_login_change(store):
    from tuman.config import Settings
    account=store.one("SELECT * FROM accounts WHERE role='china'")
    store.change_credentials(account["id"],TEST_PASSWORD,"china_new","", "none")
    store.bootstrap(Settings(passwords={"china": TEST_PASSWORD}))
    assert store.one("SELECT COUNT(*) n FROM accounts WHERE role='china'")["n"]==1
    assert store.account(account["id"])["username"]=="china_new"

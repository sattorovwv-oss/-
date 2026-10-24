from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import sqlite3
import uuid

import pytest

from tuman.db import Store
from tuman.security import minor, number, password_hash, password_matches, track_code
from .conftest import TEST_PASSWORD


def aid(store, role):
    return store.one("SELECT id FROM accounts WHERE username=?", (role,))["id"]


def scan(store, role, track="TRACK123", request_id=None):
    return store.scan(aid(store, role), role, track, request_id or str(uuid.uuid4()))


@pytest.mark.parametrize("value", ["", "12345", "A"*41, "ТЕСТ1234", "ABC/1234", "<script>"])
def test_invalid_tracks(value):
    with pytest.raises(ValueError): track_code(value)


def test_track_normalization():
    assert track_code(" ab c-12\n34 ") == "ABC-1234"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-2", "0", "1000001", "abc"])
def test_invalid_weights(value):
    with pytest.raises(ValueError): number(value)


def test_money_uses_decimal_rounding():
    assert minor("1,005") == 101
    assert number(" 2,5 ") == Decimal("2.5")


def test_passwords_salted_and_not_plaintext():
    a, b = password_hash(TEST_PASSWORD), password_hash(TEST_PASSWORD)
    assert a != b and TEST_PASSWORD not in a
    assert password_matches(TEST_PASSWORD, a)
    assert not password_matches("incorrect", a)
    with pytest.raises(ValueError): password_hash("short")


def test_two_warehouses_and_no_regression(store):
    assert scan(store, "china")["stage"] == 10
    assert scan(store, "tajik")["stage"] == 30
    assert scan(store, "china")["outcome"] == "ahead"
    assert store.parcel("TRACK123")["stage"] == 30
    assert store.one("SELECT COUNT(*) n FROM parcels")["n"] == 1


def test_tajik_can_receive_without_china_scan(store):
    assert scan(store, "tajik")["stage"] == 30


def test_repeated_request_is_idempotent(store):
    request_id = str(uuid.uuid4())
    first = scan(store, "china", request_id=request_id)
    assert scan(store, "china", request_id=request_id) == first
    assert scan(store, "china")["outcome"] == "duplicate"
    assert store.one("SELECT COUNT(*) n FROM events WHERE action='scan'")["n"] == 1
    with pytest.raises(ValueError): scan(store, "china", "DIFFERENT123", request_id)


def test_cannot_scan_other_warehouse_or_as_customer(store, customer):
    with pytest.raises(PermissionError): store.scan(aid(store,"china"), "tajik", "TRACK123",str(uuid.uuid4()))
    with pytest.raises(PermissionError): store.scan(customer["id"], "china", "TRACK123",str(uuid.uuid4()))


def test_concurrent_scans_keep_furthest_stage(store):
    jobs = ["china", "tajik"]*12
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda role: scan(store, role), jobs))
    assert len(results) == 24
    assert store.parcel("TRACK123")["stage"] == 30
    assert store.one("SELECT COUNT(*) n FROM parcels")["n"] == 1


def test_concurrent_retries_of_same_scan(store):
    rid = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: scan(store,"china",request_id=rid), range(16)))
    assert all(r == results[0] for r in results)
    assert store.one("SELECT COUNT(*) n FROM scan_requests")["n"] == 1


def test_claim_approval_and_competing_claim(store, customer):
    p = store.telegram_person(101, "Владелец",language="ru")
    other = store.telegram_person(202, "Другой",language="tj")
    assert store.add_track(p["id"], "TRACK123") == "pending"
    assert store.add_track(other["id"], "TRACK123") == "pending"
    assert store.parcel("TRACK123")["owner_id"] is None
    claim = store.one("SELECT id FROM claims WHERE person_id=?",(p["id"],))
    with pytest.raises(PermissionError): store.decide_claim(claim["id"],customer["id"])
    store.decide_claim(claim["id"],aid(store,"china"))
    assert store.parcel("TRACK123")["owner_id"] == p["id"]
    assert store.add_track(other["id"],"TRACK123") == "taken"
    assert store.one("SELECT state FROM claims WHERE person_id=?",(other["id"],))["state"] == "rejected"


@pytest.mark.parametrize("scan_first", [True,False])
def test_scan_and_claim_orders_work(store, scan_first):
    p = store.telegram_person(101,"Client",language="ru")
    if scan_first: scan(store,"china")
    store.add_track(p["id"],"TRACK123")
    claim = store.one("SELECT id FROM claims")
    store.decide_claim(claim["id"],aid(store,"china"))
    if not scan_first: scan(store,"china")
    parcel = store.parcel("TRACK123")
    assert parcel["stage"] == 10 and parcel["owner_id"] == p["id"]
    messages = store.rows("SELECT * FROM outbox")
    assert messages and all(m["chat_id"]==101 for m in messages)
    assert "На складе в Китае" in messages[-1]["text"]


def test_new_warehouse_notification_and_no_duplicate(store):
    p = store.telegram_person(101,"Client",language="ru")
    store.add_track(p["id"],"TRACK123")
    store.decide_claim(store.one("SELECT id FROM claims")["id"],aid(store,"china"))
    scan(store,"china"); scan(store,"china"); scan(store,"tajik"); scan(store,"china")
    messages=store.rows("SELECT * FROM outbox WHERE event_key LIKE 'stage:%'")
    assert len(messages)==2
    assert "Таджикистане" in messages[-1]["text"]


def test_warehouse_edits_and_delivery_rules(store):
    scan(store,"china")
    parcel=store.parcel("TRACK123")
    store.set_preferences("42.5","сомонӣ")
    store.edit_parcel(parcel["id"],aid(store,"china"),"2,5","",False,20)
    assert store.parcel("TRACK123")["price_minor"]==10625
    scan(store,"tajik")
    with pytest.raises(PermissionError): store.edit_parcel(parcel["id"],aid(store,"china"),"2.5","100",False,20)
    store.edit_parcel(parcel["id"],aid(store,"tajik"),"2.5","100",True,40)
    assert scan(store,"tajik")["outcome"]=="ahead"
    assert store.parcel("TRACK123")["stage"]==40


def test_archive_preserves_audit_and_blocks_reuse(store):
    scan(store,"china")
    p=store.parcel("TRACK123")
    with pytest.raises(PermissionError): store.archive_parcel(p["id"],aid(store,"china"))
    store.archive_parcel(p["id"],aid(store,"owner"))
    assert store.parcel("TRACK123") is None
    assert store.one("SELECT COUNT(*) n FROM events")["n"]==2
    with pytest.raises(ValueError): scan(store,"china")


def test_backup_includes_wal_and_can_be_restored(store,tmp_path):
    scan(store,"china")
    backup=store.backup(tmp_path/"backups")
    restored=Store(backup)
    assert restored.parcel("TRACK123")["stage"]==10
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"

import asyncio
import uuid
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from tuman.workers import deliver_once


def queued(store):
    person=store.telegram_person(101,"Client",language="ru")
    store.add_track(person["id"],"TRACK123")
    store.decide_claim(store.one("SELECT id FROM claims")["id"],store.one("SELECT id FROM accounts WHERE role='china'")["id"])


class Sender:
    def __init__(self,error=None): self.calls=[]; self.error=error
    async def send_message(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        if self.error: raise self.error


def test_outbox_sends_and_does_not_resend_acknowledged_messages(store):
    queued(store); bot=Sender()
    asyncio.run(deliver_once(store,bot)); asyncio.run(deliver_once(store,bot))
    assert len(bot.calls)==1
    assert store.one("SELECT state FROM outbox")["state"]=="sent"


def test_temporary_failure_is_retained_and_retried(store):
    queued(store)
    asyncio.run(deliver_once(store,Sender(ConnectionError("network"))))
    assert store.one("SELECT state FROM outbox")["state"]=="pending"
    with store.connection(True) as db: db.execute("UPDATE outbox SET next_at=0")
    bot=Sender(); asyncio.run(deliver_once(store,bot))
    assert len(bot.calls)==1
    assert store.one("SELECT attempts FROM outbox")["attempts"]==2


def test_blocked_client_does_not_lose_parcel(store):
    queued(store)
    error=TelegramForbiddenError(method=SendMessage(chat_id=101,text="x"),message="blocked")
    asyncio.run(deliver_once(store,Sender(error)))
    assert store.one("SELECT state FROM outbox")["state"]=="blocked"
    assert store.parcel("TRACK123") is not None


def test_rate_limit_defers_batch(store):
    queued(store)
    error=TelegramRetryAfter(method=SendMessage(chat_id=101,text="x"),message="rate limit",retry_after=20)
    asyncio.run(deliver_once(store,Sender(error)))
    row=store.one("SELECT * FROM outbox")
    assert row["state"]=="pending" and row["next_at"]>row["created"]+19


def test_obsolete_china_alert_is_not_sent_after_tajik_arrival(store):
    queued(store)
    china=store.one("SELECT id FROM accounts WHERE role='china'")["id"]
    tajik=store.one("SELECT id FROM accounts WHERE role='tajik'")["id"]
    store.scan(china,"china","TRACK123",str(uuid.uuid4()))
    store.scan(tajik,"tajik","TRACK123",str(uuid.uuid4()))
    bot=Sender(); asyncio.run(deliver_once(store,bot))
    assert len(bot.calls)==2  # current claim card and current Tajik arrival
    assert all("На складе в Таджикистане" in args[1] for args,kwargs in bot.calls)
    assert store.one("SELECT state FROM outbox WHERE event_key LIKE 'stage:%:10'")["state"]=="superseded"

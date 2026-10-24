import asyncio
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from tuman.bot import build_dispatcher, keyboard
from tuman.config import Settings


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent=[]

    async def close(self): pass

    async def make_request(self,bot,method,timeout=None):
        self.sent.append(method)
        if isinstance(method,SendMessage):
            return Message(message_id=len(self.sent),date=datetime.now(timezone.utc),chat=Chat(id=int(method.chat_id),type="private"),text=method.text)
        return True

    async def stream_content(self,*args,**kwargs):
        yield b""


async def feed(dp,bot,text,user_id=101,chat_type="private"):
    await dp.feed_update(bot,Update(update_id=int(datetime.now().timestamp()*1000),message=Message(message_id=1,date=datetime.now(timezone.utc),chat=Chat(id=user_id,type=chat_type),from_user=User(id=user_id,is_bot=False,first_name="Client"),text=text)))


def test_bot_start_add_search_and_language(store):
    async def run():
        session=RecordingSession()
        bot=Bot("123456:TEST_ONLY_NOT_A_REAL_BOT_TOKEN",session=session)
        dp=build_dispatcher(store,Settings())
        await feed(dp,bot,"/start")
        assert "Выберите язык" in session.sent[-1].text
        store.telegram_person(101,"Client",language="ru")
        await feed(dp,bot,"➕ Новая посылка")
        await feed(dp,bot,"track123")
        assert "Заявка" in session.sent[-1].text
        await feed(dp,bot,"🔍 Поиск")
        await feed(dp,bot,"TRACK123")
        assert "Ожидается" in session.sent[-1].text
        await feed(dp,bot,"/cancel")
        assert session.sent[-1].text=="👌"
        await dp.storage.close()
    asyncio.run(run())


def test_bot_admin_access_and_one_time_link(store):
    async def run():
        session=RecordingSession(); bot=Bot("123456:TEST_ONLY_NOT_A_REAL_BOT_TOKEN",session=session)
        dp=build_dispatcher(store,Settings(public_url="https://cargo.example"))
        await feed(dp,bot,"/panel")
        assert store.one("SELECT COUNT(*) n FROM login_tokens")["n"]==0
        with store.connection(True) as db:
            aid=store.one("SELECT id FROM accounts WHERE role='china'")["id"]
            db.execute("INSERT INTO telegram_admins VALUES (101,?)",(aid,))
        await feed(dp,bot,"/panel")
        button=session.sent[-1].reply_markup.inline_keyboard[0][0]
        assert button.url.startswith("https://cargo.example/auth/telegram#token=")
        assert store.one("SELECT COUNT(*) n FROM login_tokens")["n"]==1
        await dp.storage.close()
    asyncio.run(run())


def test_bot_ignores_groups_and_colours_danger_button(store):
    async def run():
        session=RecordingSession(); bot=Bot("123456:TEST_ONLY_NOT_A_REAL_BOT_TOKEN",session=session)
        dp=build_dispatcher(store,Settings())
        await feed(dp,bot,"/panel",chat_type="group")
        assert not session.sent
        assert keyboard("ru").keyboard[2][0].style=="danger"
        await dp.storage.close()
    asyncio.run(run())


def test_bot_support_and_calculator(store):
    async def run():
        store.telegram_person(101,"Client",language="ru")
        store.set_preferences("42.5","TJS")
        session=RecordingSession(); bot=Bot("123456:TEST_ONLY_NOT_A_REAL_BOT_TOKEN",session=session)
        dp=build_dispatcher(store,Settings())
        await feed(dp,bot,"💵 Цена"); await feed(dp,bot,"2,5")
        assert "106.25" in session.sent[-1].text
        await feed(dp,bot,"🆘 Поддержка"); await feed(dp,bot,"Где посылка?")
        assert store.one("SELECT COUNT(*) n FROM tickets")["n"]==1
        await dp.storage.close()
    asyncio.run(run())

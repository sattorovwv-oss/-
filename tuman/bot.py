from decimal import Decimal, ROUND_HALF_UP
from html import escape
import logging
import time

from aiogram import Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart, ExceptionTypeFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

from .security import display, number, track_code
from .texts import BUTTONS, card, tr

logger = logging.getLogger(__name__)


class Dialogue(StatesGroup):
    search = State()
    calculator = State()
    support = State()
    reply = State()
    broadcast = State()
    rate = State()


def keyboard(lang, admin=False):
    idx = 0 if lang == "ru" else 1
    keys = []
    for left, right in (("search", "add"), ("my", "price"), ("support", "language")):
        keys.append([KeyboardButton(text=BUTTONS[left][idx], style="danger" if left == "support" else "primary"), KeyboardButton(text=BUTTONS[right][idx], style="success" if right == "add" else None)])
    keys.append([KeyboardButton(text=BUTTONS["website"][idx], style="primary")])
    if admin:
        keys.append([KeyboardButton(text=BUTTONS["panel"][idx], style="danger")])
    return ReplyKeyboardMarkup(keyboard=keys, resize_keyboard=True)


def ticket_keyboard(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ҷавоб додан", callback_data=f"reply:{ticket_id}", style="primary"),
        InlineKeyboardButton(text="🚫 Рад кардан", callback_data=f"reject:{ticket_id}", style="danger"),
    ]])


def build_dispatcher(store, settings):
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    @router.errors(ExceptionTypeFilter(TelegramNetworkError))
    async def connection_interrupted(event: ErrorEvent):
        update = event.update
        message = update.message
        callback = update.callback_query
        user = None
        if message and message.chat.type == ChatType.PRIVATE:
            user = message.from_user
        elif callback and callback.message and callback.message.chat.type == ChatType.PRIVATE:
            user = callback.from_user
        if user:
            p = store.one("SELECT lang FROM people WHERE telegram_id=?", (user.id,))
            store.queue_connection_notice(user.id, update.update_id, tr("network_recovery", p["lang"] if p else "tj"))
        logger.warning("Telegram connection interrupted while replying; recovery notice queued where possible. Saved data is retained.")
        return True

    def person(message, language=None):
        user = message.from_user
        return store.telegram_person(user.id, user.full_name, user.username or "", language)

    async def choose(message):
        return await message.answer("Забонро интихоб кунед / Выберите язык", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🇹🇯 Тоҷикӣ", callback_data="lang:tj", style="primary"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru", style="primary"),
        ]]))

    async def welcome(message, p):
        await message.answer(tr("welcome", p["lang"]), reply_markup=keyboard(p["lang"], bool(store.admin_for_telegram(message.from_user.id))))

    async def panel(message):
        account = store.admin_for_telegram(message.from_user.id)
        if not account:
            await message.answer(tr("denied", "ru"))
            return
        rows = []
        if settings.has_public_web_url():
            token = store.issue_link(account_id=account["id"])
            rows.append([InlineKeyboardButton(text="🖥 Кушодани панели анбор", url=settings.public_url+"/auth/telegram#token="+token, style="primary")])
            text = "Пайванди шахсӣ барои 2 дақиқа. Онро ба каси дигар нафиристед.\nСсылка открывает ваш аккаунт без повторного ввода пароля."
        else:
            text = tr("web_local", "tj") + "\n\n" + tr("web_local", "ru") + "\n\nДля кнопки входа укажите публичный HTTPS-адрес сайта в PUBLIC_URL."
        rows.append([InlineKeyboardButton(text="📊 Омор", callback_data="admin:stats")])
        if account["role"] == "owner":
            rows.append([InlineKeyboardButton(text="💵 Нарх", callback_data="admin:rate", style="success"), InlineKeyboardButton(text="📣 Эълон", callback_data="admin:broadcast", style="danger")])
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def website(message, p):
        if not settings.has_public_web_url():
            await message.answer(tr("web_local", p["lang"]))
            return
        token = store.issue_link(person_id=p["id"])
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌍 Кушодан / Открыть кабинет", url=settings.public_url+"/auth/telegram#token="+token, style="primary")],
            [InlineKeyboardButton(text="Бақайдгирӣ / Регистрация", url=settings.public_url+"/register")],
        ])
        await message.answer("Если это первое подключение: зарегистрируйтесь на сайте, войдите в аккаунт, затем получите новую ссылку командой /website и откройте её в том же браузере.\n\nПосле привязки кнопка сразу откроет ваш кабинет. Ссылка личная и действует 2 минуты.", reply_markup=markup)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        p = person(message)
        await welcome(message, p) if p["lang"] else await choose(message)

    @router.message(Command("id"))
    async def user_id(message: Message):
        person(message)
        await message.answer(f"Telegram ID: <code>{message.from_user.id}</code>")

    @router.message(Command("language"))
    async def language(message: Message, state: FSMContext):
        await state.clear()
        person(message)
        await choose(message)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        p = person(message)
        await message.answer("👌", reply_markup=keyboard(p["lang"], bool(store.admin_for_telegram(message.from_user.id))))

    @router.message(Command("admin", "panel"))
    async def admin(message: Message, state: FSMContext):
        await state.clear()
        person(message)
        await panel(message)

    @router.message(Command("website"))
    async def site(message: Message, state: FSMContext):
        await state.clear()
        await website(message, person(message))

    async def my_parcels(message, p):
        parcels = store.list_parcels(person_id=p["id"], limit=40)
        if not parcels:
            await message.answer(tr("empty", p["lang"]))
        else:
            # Send separate cards in bounded chunks to respect Telegram's 4096 character limit.
            currency = store.preferences()["currency"]
            chunk = ""
            for parcel in parcels:
                line = card(parcel, p["lang"], currency=currency) + "\n\n"
                if len(chunk) + len(line) > 3500:
                    await message.answer(chunk.rstrip())
                    chunk = ""
                chunk += line
            if chunk:
                await message.answer(chunk.rstrip())

    @router.message(Command("my"))
    async def my(message: Message, state: FSMContext):
        await state.clear()
        p = person(message)
        if not p["lang"]:
            await choose(message)
        else:
            await my_parcels(message, p)

    @router.callback_query(F.data.startswith("lang:"))
    async def set_language(callback: CallbackQuery, state: FSMContext):
        lang = callback.data.split(":", 1)[1]
        if lang not in ("ru", "tj"):
            await callback.answer()
            return
        user = callback.from_user
        p = store.telegram_person(user.id, user.full_name, user.username or "", lang)
        await state.clear()
        await callback.answer()
        await callback.message.answer(tr("welcome", lang), reply_markup=keyboard(lang, bool(store.admin_for_telegram(user.id))))

    @router.callback_query(F.data.startswith("admin:"))
    async def admin_action(callback: CallbackQuery, state: FSMContext):
        account = store.admin_for_telegram(callback.from_user.id)
        if not account:
            await callback.answer("Дастрасӣ нест", show_alert=True)
            return
        action = callback.data.split(":",1)[1]
        await state.clear()
        await callback.answer()
        if action == "stats":
            s = store.stats()
            await callback.message.answer(f"📊 Омор\nБорҳо: {s['total']}\nЧин: {s['china']}\nДар роҳ: {s['transit']}\nТоҷикистон: {s['tajik']}\nСупорида шуд: {s['delivered']}\nДархостҳо: {s['claims']}\nПаёмҳо: {s['tickets']}")
        elif account["role"] == "owner" and action == "broadcast":
            await state.set_state(Dialogue.broadcast)
            await callback.message.answer("Матни эълонро фиристед (то 3000 аломат). /cancel — бекор кардан.")
        elif account["role"] == "owner" and action == "rate":
            await state.set_state(Dialogue.rate)
            await callback.message.answer("Нархи нави 1 кг-ро фиристед. /cancel — бекор кардан.")

    @router.callback_query(F.data.startswith("broadcast:"))
    async def confirm_broadcast(callback: CallbackQuery, state: FSMContext):
        account = store.admin_for_telegram(callback.from_user.id)
        data = await state.get_data()
        await callback.answer()
        if not account or account["role"] != "owner" or not data.get("broadcast"):
            await callback.message.answer("Эълон аллакай коркард шуд ё иҷозат нест.")
            return
        text = data["broadcast"]
        await state.clear()
        if callback.data == "broadcast:yes":
            count = store.broadcast(text, account["id"])
            await callback.message.answer(f"✅ Дар навбат: {count}")
        else:
            await callback.message.answer("👌 Бекор шуд")

    @router.callback_query(F.data.startswith("reply:"))
    async def support_reply(callback: CallbackQuery, state: FSMContext):
        if not store.admin_for_telegram(callback.from_user.id):
            await callback.answer("Дастрасӣ нест", show_alert=True)
            return
        await state.set_state(Dialogue.reply)
        await state.update_data(ticket_id=int(callback.data.split(":")[1]))
        await callback.answer()
        await callback.message.answer("Матни ҷавобро фиристед. /cancel — бекор кардан.")

    @router.callback_query(F.data.startswith("reject:"))
    async def support_reject(callback: CallbackQuery, state: FSMContext):
        account = store.admin_for_telegram(callback.from_user.id)
        if not account:
            await callback.answer("Дастрасӣ нест", show_alert=True)
            return
        try:
            store.answer_ticket(int(callback.data.split(":")[1]), account["id"], "", True)
            await callback.answer("Рад шуд")
        except ValueError as exc:
            await callback.answer(str(exc)[:180], show_alert=True)

    @router.message(F.text)
    async def text(message: Message, state: FSMContext):
        p = person(message)
        lang = p["lang"]
        if not lang:
            await choose(message)
            return
        value = message.text
        action = next((key for key, labels in BUTTONS.items() if value in labels), None)
        if action:
            await state.clear()
            if action == "language":
                await choose(message)
            elif action == "add":
                await message.answer(tr("add", lang))
            elif action == "search":
                await state.set_state(Dialogue.search)
                await message.answer(tr("search", lang))
            elif action == "my":
                await my_parcels(message, p)
            elif action == "price":
                prefs = store.preferences()
                rate = int(prefs["rate_minor"])
                if not rate:
                    await message.answer(tr("unset", lang))
                else:
                    await state.set_state(Dialogue.calculator)
                    await message.answer("💵 1 кг = " + display(Decimal(rate)/100) + " " + escape(prefs["currency"]) + "\n\n" + tr("weight", lang))
            elif action == "support":
                await state.set_state(Dialogue.support)
                await message.answer(tr("support", lang))
            elif action == "website":
                await website(message, p)
            elif action == "panel":
                await panel(message)
            return
        if value.startswith("/"):
            await state.clear()
            await message.answer("/start · /my · /language · /cancel")
            return
        current = await state.get_state()
        try:
            if current == Dialogue.search.state:
                parcel = store.parcel(value)
                await message.answer(card(parcel, lang, full=parcel["owner_id"] == p["id"], currency=store.preferences()["currency"]) if parcel else tr("missing", lang))
            elif current == Dialogue.calculator.state:
                kg = number(value)
                prefs = store.preferences()
                rate = Decimal(prefs["rate_minor"])/100
                total = (kg*rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                await message.answer(f"⚖️ {display(kg)} кг × {display(rate)} = {display(total)} {escape(prefs['currency'])}")
            elif current == Dialogue.support.state:
                if store.limited(f"tg-support:{message.from_user.id}", limit=10, seconds=3600):
                    raise ValueError("Слишком много обращений. Повторите позже.")
                store.create_ticket(p["id"], value)
                await state.clear()
                await message.answer(tr("ticket", lang))
            elif current in (Dialogue.reply.state, Dialogue.broadcast.state, Dialogue.rate.state):
                account = store.admin_for_telegram(message.from_user.id)
                if not account:
                    raise ValueError(tr("denied", lang))
                if current == Dialogue.reply.state:
                    data = await state.get_data()
                    store.answer_ticket(data["ticket_id"], account["id"], value)
                    await state.clear()
                    await message.answer("✅ Ҷавоб сабт шуд")
                elif account["role"] != "owner":
                    raise ValueError(tr("denied", lang))
                elif current == Dialogue.rate.state:
                    store.set_preferences(value, store.preferences()["currency"])
                    await state.clear()
                    await message.answer("✅ Нарх сабт шуд")
                else:
                    if not value.strip() or len(value) > 3000:
                        raise ValueError("Матн: 1–3000 аломат")
                    await state.update_data(broadcast=value)
                    await message.answer("Пешнамоиш / Предпросмотр:\n\n" + escape(value), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="↗ Фиристодан", callback_data="broadcast:yes", style="success"),
                        InlineKeyboardButton(text="Бекор кардан", callback_data="broadcast:no", style="danger"),
                    ]]))
            else:
                if store.limited(f"tg-tracks:{message.from_user.id}", limit=50, seconds=3600):
                    raise ValueError("Слишком много заявок. Повторите позже.")
                result = store.add_track(p["id"], value)
                parcel = store.parcel(value)
                await message.answer(tr("claim", lang) if result == "pending" else tr("taken", lang) if result == "taken" else card(parcel, lang, currency=store.preferences()["currency"]))
        except (ValueError, PermissionError) as exc:
            await message.answer(escape(str(exc)))

    @router.message()
    async def unsupported(message: Message):
        p = person(message)
        await message.answer(tr("notext", p["lang"]))

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp

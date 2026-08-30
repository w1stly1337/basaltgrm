import asyncio
import functools
import json
import logging
import os
import random
import sqlite3
import time
import traceback

try:
    import json_repair
except ImportError:
    json_repair = None

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.command import Command

TOKEN = os.environ.get('BOT_TOKEN', '8814729828:AAEIDJwTUEX8pOS_jevPhIM9TbURZLayzss')
DB_FILE = 'bot.db'
DATA_FILE = 'numbers.json'
BACKUP_FILE = 'numbers.json.bak'
LOCK_FILE = 'bot.lock'

ADMIN_IDS = {
    int(x) for x in os.environ.get('ADMIN_IDS', '6001078667,2024447637').split(',')
    if x.strip().isdigit()
}

SUPPORT_TOPICS = {
    'bug': '🐞 Баг или проблема',
    'donate': '💰 Вопрос по донату',
}

CHANNELS = [
    ('BasaltGram News', 'http://t.me/basaltgram'),
]

FREE_PREFIX = '1'
ANON_PREFIX = '888'

NUM_FORMATS = {
    'free': (FREE_PREFIX, 111_111_1111, 999_999_9999),
    'anon': (ANON_PREFIX, 1111_1111, 9999_9999),
    'shortanon': (ANON_PREFIX, 1111, 9999),
}

MAX_GEN_ATTEMPTS = 1000
MAX_FORWARD_MAP = 10000

MAIN_TEXT = (
    '<b>👋 Привет! Это бот для авторизации в BasaltGram.</b>\n\n'
    'ℹ️ Здесь ты можешь приобрести анонимный номер за звезды, либо взять бесплатный номер. '
    'Все коды, отправленные в BasaltGram на приобретенные тобой номера, будут отображаться здесь.\n\n'
    '<b>🕹 Используй кнопки ниже для управления ботом.</b>'
)
SUB_TEXT = '<b>🕹 Подпишись на следующие каналы для продолжения работы с ботом.</b>'

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid INTEGER PRIMARY KEY,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS numbers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid INTEGER NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
    number TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS support_topics (
    uid INTEGER PRIMARY KEY,
    topic TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS support_forward (
    fkey TEXT PRIMARY KEY,
    uid INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    uid INTEGER NOT NULL,
    username TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    amount INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_numbers_uid ON numbers(uid);
"""


class Database:
    def __init__(self, path: str = DB_FILE) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> None:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.executescript(SCHEMA)
        conn.commit()
        self._migrate_from_json(conn)
        self._conn = conn

    def _migrate_from_json(self, conn: sqlite3.Connection) -> None:
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            return

        data = Database._parse_json_file(DATA_FILE)
        if data is None:
            data = Database._parse_json_file(BACKUP_FILE)
        if not data:
            return

        inserted = 0
        for raw_uid, nums in data.items():
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if not isinstance(nums, list):
                continue
            for n in nums:
                if not isinstance(n, (str, int)):
                    continue
                num = str(n)
                try:
                    conn.execute('INSERT OR IGNORE INTO users(uid) VALUES (?)', (uid,))
                    conn.execute('INSERT OR IGNORE INTO numbers(uid, number) VALUES (?, ?)', (uid, num))
                    inserted += 1
                except sqlite3.IntegrityError:
                    continue
        conn.commit()
        if inserted:
            logging.warning('Импортировано %s номеров из %s в SQLite', inserted, DATA_FILE)

    @staticmethod
    def _parse_json_file(path: str) -> dict | None:
        try:
            with open(path, 'r', encoding='utf-8') as file:
                raw = file.read()
        except (FileNotFoundError, OSError):
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if json_repair is None:
                return None
            try:
                data = json_repair.loads(raw)
            except Exception:
                return None
        return data if isinstance(data, dict) else None

    async def shutdown(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    async def ensure_user(self, uid: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._ensure_user_sync, uid)

    def _ensure_user_sync(self, uid: int) -> None:
        self._conn.execute('INSERT OR IGNORE INTO users(uid) VALUES (?)', (uid,))
        self._conn.commit()

    async def get_user(self, uid: int) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(self._get_user_sync, uid)

    def _get_user_sync(self, uid: int) -> list[str]:
        cur = self._conn.execute('SELECT number FROM numbers WHERE uid = ? ORDER BY id', (uid,))
        return [row[0] for row in cur.fetchall()]

    async def has_free_number(self, uid: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._has_free_number_sync, uid)

    def _has_free_number_sync(self, uid: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM numbers WHERE uid = ? AND number LIKE ? LIMIT 1",
            (uid, FREE_PREFIX + '%'),
        )
        return cur.fetchone() is not None

    async def add_number(self, uid: int, prefix: str, low: int, high: int) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._add_number_sync, uid, prefix, low, high)

    def _add_number_sync(self, uid: int, prefix: str, low: int, high: int) -> str:
        for _ in range(MAX_GEN_ATTEMPTS):
            number = f'{prefix}{random.randint(low, high)}'
            try:
                self._conn.execute('INSERT OR IGNORE INTO users(uid) VALUES (?)', (uid,))
                self._conn.execute('INSERT INTO numbers(uid, number) VALUES (?, ?)', (uid, number))
                self._conn.commit()
                return number
            except sqlite3.IntegrityError:
                self._conn.rollback()
        raise RuntimeError('Не удалось сгенерировать уникальный номер')

    async def get_all_users(self) -> dict[str, list[str]]:
        async with self._lock:
            return await asyncio.to_thread(self._get_all_users_sync)

    def _get_all_users_sync(self) -> dict[str, list[str]]:
        users: dict[str, list[str]] = {}
        cur = self._conn.execute('SELECT uid, number FROM numbers ORDER BY uid, id')
        for uid, number in cur.fetchall():
            users.setdefault(str(uid), []).append(number)
        return users

    async def get_topic(self, uid: int) -> str | None:
        async with self._lock:
            cur = self._conn.execute('SELECT topic FROM support_topics WHERE uid = ?', (uid,))
            row = cur.fetchone()
            return row[0] if row else None

    async def set_topic(self, uid: int, topic: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_topic_sync, uid, topic)

    def _set_topic_sync(self, uid: int, topic: str) -> None:
        self._conn.execute('INSERT OR REPLACE INTO support_topics(uid, topic) VALUES (?, ?)', (uid, topic))
        self._conn.commit()

    async def close_support(self, uid: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close_support_sync, uid)

    def _close_support_sync(self, uid: int) -> None:
        self._conn.execute('DELETE FROM support_topics WHERE uid = ?', (uid,))
        self._conn.commit()

    async def open_count(self) -> int:
        async with self._lock:
            cur = self._conn.execute('SELECT COUNT(*) FROM support_topics')
            return cur.fetchone()[0]

    async def add_forward(self, admin_id: int, message_id: int, uid: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._add_forward_sync, admin_id, message_id, uid)

    def _add_forward_sync(self, admin_id: int, message_id: int, uid: int) -> None:
        fkey = f'{admin_id}:{message_id}'
        self._conn.execute('INSERT OR REPLACE INTO support_forward(fkey, uid) VALUES (?, ?)', (fkey, uid))
        self._conn.execute(
            'DELETE FROM support_forward WHERE fkey NOT IN '
            '(SELECT fkey FROM support_forward ORDER BY rowid DESC LIMIT ?)',
            (MAX_FORWARD_MAP,),
        )
        self._conn.commit()

    async def pop_uid(self, admin_id: int, message_id: int) -> int | None:
        async with self._lock:
            return await asyncio.to_thread(self._pop_uid_sync, admin_id, message_id)

    def _pop_uid_sync(self, admin_id: int, message_id: int) -> int | None:
        fkey = f'{admin_id}:{message_id}'
        cur = self._conn.execute('SELECT uid FROM support_forward WHERE fkey = ?', (fkey,))
        row = cur.fetchone()
        if row is None:
            return None
        self._conn.execute('DELETE FROM support_forward WHERE fkey = ?', (fkey,))
        self._conn.commit()
        return row[0]

    async def log_payment(self, uid: int, username: str | None, payload: str, amount: int) -> None:
        async with self._lock:
            await asyncio.to_thread(self._log_payment_sync, uid, username, payload, amount)

    def _log_payment_sync(self, uid: int, username: str | None, payload: str, amount: int) -> None:
        self._conn.execute(
            'INSERT INTO payments(ts, uid, username, payload, amount) VALUES (?, ?, ?, ?, ?)',
            (time.time(), uid, username or '', payload, amount),
        )
        self._conn.commit()

    async def payment_total(self) -> int:
        async with self._lock:
            cur = self._conn.execute('SELECT COALESCE(SUM(amount), 0) FROM payments')
            return cur.fetchone()[0]


db = Database()


def acquire_lock(path: str):
    if os.name == 'nt':
        import msvcrt

        handle = open(path, 'a+')
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write('\0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return handle
        except OSError:
            handle.close()
            return None
    else:
        import fcntl

        handle = open(path, 'a+')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            handle.close()
            return None


async def missing_channels(uid: int) -> list[tuple[str, str]]:
    missing = []
    for name, url in CHANNELS:
        username = url.replace('http://t.me/', '@')
        try:
            member = await bot.get_chat_member(username, uid)
        except TelegramBadRequest:
            continue
        if member.status == 'left':
            missing.append((name, url))
    return missing


async def start_content(uid: int) -> tuple[str, types.InlineKeyboardMarkup]:
    if missing := await missing_channels(uid):
        keyboard = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text=f'📢 {name}', url=url) for name, url in missing],
                [types.InlineKeyboardButton(text='✅ Я подписался', callback_data='check_sub')],
            ]
        )
        return SUB_TEXT, keyboard

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='📱 Получить бесплатный номер', callback_data='freenum')],
            [types.InlineKeyboardButton(text='📱 Купить анонимный номер', callback_data='anon')],
            [types.InlineKeyboardButton(text='📱 Купить короткий анонимный номер', callback_data='shortanon')],
            [types.InlineKeyboardButton(text='📊 Мои номера', callback_data='mynums')],
            [types.InlineKeyboardButton(text='⭐ Купить звёзды', callback_data='buystars')],
            [types.InlineKeyboardButton(text='✅ Купить верификацию', callback_data='buyverify')],
            [types.InlineKeyboardButton(text='👑 Купить премиум', callback_data='buypremium')],
            [types.InlineKeyboardButton(text='🎨 Купить NFT юзернейм', callback_data='buynft')],
            [types.InlineKeyboardButton(text='🛠 Поддержка', callback_data='support')],
        ]
    )
    return MAIN_TEXT, keyboard


def safe_callback_handler(handler):
    @functools.wraps(handler)
    async def wrapper(query: types.CallbackQuery, *args, **kwargs):
        try:
            await handler(query, *args, **kwargs)
        except Exception:
            logging.exception('Ошибка в обработчике кнопки: %s', query.data)
            try:
                await query.answer()
            except Exception:
                pass
            try:
                await query.message.answer('<b>⚠️ Произошла ошибка. Попробуй ещё раз.</b>')
            except Exception:
                pass
    return wrapper


@dp.errors()
async def on_bot_error(event: types.ErrorEvent) -> None:
    exc = event.exception
    tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logging.error('Ошибка при обработке update:\n%s', tb)

    update_snippet = str(event.update)[:1200] if event.update is not None else 'update=None'
    report = f'<b>⚠️ Ошибка бота</b>\n\n<code>{tb[-1500:]}</code>\n\nUpdate:\n<code>{update_snippet}</code>'
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, report)
        except TelegramBadRequest:
            pass


@dp.message(Command('start'))
async def start(message: types.Message) -> None:
    text, keyboard = await start_content(message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data == 'check_sub')
async def check_sub(query: types.CallbackQuery) -> None:
    await query.answer()
    text, keyboard = await start_content(query.from_user.id)
    try:
        await query.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == 'freenum')
@safe_callback_handler
async def freenum(query: types.CallbackQuery) -> None:
    await query.answer()
    await db.ensure_user(query.from_user.id)

    if await db.has_free_number(query.from_user.id):
        await query.message.answer('<b>❌ У вас уже есть бесплатный номер!</b>')
        return

    number = await db.add_number(query.from_user.id, *NUM_FORMATS['free'])
    await query.message.answer(f'<b>✅ Успешно добавлен бесплатный номер!\n\n📱 Номер:</b>\n<pre>+{number}</pre>')


@dp.callback_query(F.data == 'anon')
async def anon(query: types.CallbackQuery) -> None:
    await query.answer()
    await query.message.answer_invoice(
        title='📱 Анонимный номер',
        description='ℹ️ Анонимный номер для входа в BasaltGram. Пример: +888 1234 5678',
        prices=[types.LabeledPrice(label='Оплата', amount=100)],
        payload='anon',
        currency='XTR'
    )


@dp.callback_query(F.data == 'shortanon')
async def shortanon(query: types.CallbackQuery) -> None:
    await query.answer()
    await query.message.answer_invoice(
        title='📱 Короткий анонимный номер',
        description='ℹ️ Короткий анонимный номер для входа в BasaltGram. Пример: +888 1234',
        prices=[types.LabeledPrice(label='Оплата', amount=200)],
        payload='shortanon',
        currency='XTR'
    )


@dp.callback_query(F.data == 'mynums')
@safe_callback_handler
async def mynums(query: types.CallbackQuery) -> None:
    await query.answer()
    numbers = await db.get_user(query.from_user.id)
    if not numbers:
        await query.message.answer('<b>❌ У вас нет номеров.</b>')
        return

    context = ['<b>📱 Список ваших номеров</b>']
    for i, number in enumerate(numbers, 1):
        context.append(f'{i}. <code>+{number}</code>')

    text = '\n\n'.join(context)
    for part in [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]:
        await query.message.answer(part)


@dp.pre_checkout_query(F.invoice_payload.in_({'anon', 'shortanon'}))
async def checkout(query: types.PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message) -> None:
    payload = message.successful_payment.invoice_payload
    uid = message.from_user.id

    await db.ensure_user(uid)

    if payload not in NUM_FORMATS:
        await message.answer('<b>⚠️ Неизвестная оплата, обратитесь в поддержку.</b>')
        return

    number = await db.add_number(uid, *NUM_FORMATS[payload])
    await db.log_payment(uid, message.from_user.username, payload, message.successful_payment.total_amount)
    label = 'анонимный номер' if payload == 'anon' else 'короткий анонимный номер'
    await message.answer(f'<b>✅ Успешно добавлен {label}!\n\n📱 Номер:</b>\n<pre>+{number}</pre>')


@dp.message(Command('stats'))
async def admin_stats(message: types.Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return

    users = await db.get_all_users()
    free = anon = short = 0
    for numbers in users.values():
        for number in numbers:
            if number.startswith(FREE_PREFIX):
                free += 1
            elif len(number) <= 7:
                short += 1
            else:
                anon += 1

    open_tickets = await db.open_count()
    paid = await db.payment_total()

    text = (
        '<b>📊 Статистика</b>\n\n'
        f'👥 Пользователей: <b>{len(users)}</b>\n'
        f'📱 Номеров всего: <b>{free + anon + short}</b>\n'
        f'   • бесплатных: {free}\n'
        f'   • анонимных: {anon}\n'
        f'   • коротких: {short}\n'
        f'🛠 Открытых обращений: {open_tickets}\n'
        f'💰 Звёзд получено: <b>{paid}</b> ⭐'
    )
    await message.answer(text)


@dp.message(Command('broadcast'))
async def admin_broadcast(message: types.Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return

    text = message.text.removeprefix('/broadcast').strip()
    if not text:
        await message.answer('<b>Использование:</b> <code>/broadcast текст сообщения</code>')
        return

    await message.answer('<b>📢 Рассылка началась...</b>')
    sent = failed = 0
    for uid_str in await db.get_all_users():
        try:
            await bot.send_message(int(uid_str), f'<b>📢 Официальное сообщение:</b>\n\n{text}')
            sent += 1
        except TelegramBadRequest:
            failed += 1
    await message.answer(f'<b>Рассылка завершена.</b>\n✅ Отправлено: {sent}\n❌ Не доставлено: {failed}')


@dp.callback_query(F.data.in_({'buystars', 'buyverify', 'buypremium', 'buynft'}))
async def buy_offer(query: types.CallbackQuery) -> None:
    await query.answer()
    titles = {
        'buystars': '⭐ Звёзды',
        'buyverify': '✅ Верификация',
        'buypremium': '👑 Премиум',
        'buynft': '🎨 NFT юзернейм',
    }
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='👤 @w1stly1337', url='https://t.me/w1stly1337')],
            [types.InlineKeyboardButton(text='👤 @roblo_chiki', url='https://t.me/roblo_chiki')],
            [types.InlineKeyboardButton(text='◀️ Назад', callback_data='back')],
        ]
    )
    await query.message.answer(
        f'<b>💎 {titles[query.data]}</b>\n\n'
        'Чтобы приобрести — напиши одному из менеджеров:\n\n'
        '👤 <b>@w1stly1337</b>\n'
        '👤 <b>@roblo_chiki</b>\n\n'
        'Мы уже на связи 🚀',
        reply_markup=keyboard
    )


@dp.callback_query(F.data == 'support')
async def support_menu(query: types.CallbackQuery) -> None:
    await query.answer()
    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='🐞 Баг или проблема', callback_data='support_bug')],
            [types.InlineKeyboardButton(text='💰 Вопрос по донату', callback_data='support_donate')],
            [types.InlineKeyboardButton(text='◀️ Назад', callback_data='back')],
        ]
    )
    await query.message.answer('<b>🛠 Выбери тему обращения:</b>', reply_markup=keyboard)


@dp.callback_query(F.data == 'back')
async def back_to_menu(query: types.CallbackQuery) -> None:
    await query.answer()
    text, keyboard = await start_content(query.from_user.id)
    try:
        await query.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data.in_({'support_bug', 'support_donate'}))
async def choose_support_topic(query: types.CallbackQuery) -> None:
    await query.answer()
    topic = query.data.removeprefix('support_')
    await db.set_topic(query.from_user.id, topic)

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='❌ Закрыть обращение', callback_data='close_support')],
        ]
    )
    await query.message.answer(
        '<b>🛠 Режим поддержки включён.</b>\n\n'
        f'📌 Тема: {SUPPORT_TOPICS[topic]}\n\n'
        '💬 Отправь сообщение — оно уйдёт администраторам.\n'
        'Ответ админа придёт прямо сюда.\n\n'
        'Чтобы выйти — нажми «Закрыть обращение».',
        reply_markup=keyboard
    )


@dp.callback_query(F.data == 'close_support')
async def close_support(query: types.CallbackQuery) -> None:
    await query.answer()
    await db.close_support(query.from_user.id)
    await query.message.edit_text('<b>✅ Обращение закрыто.</b>')


@dp.message(F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def admin_reply_to_support(message: types.Message) -> None:
    uid = await db.pop_uid(message.chat.id, message.reply_to_message.message_id)
    if not uid:
        return

    try:
        await bot.send_message(uid, '<b>👨‍💻 Ответ администратора:</b>')
        await bot.forward_message(uid, message.chat.id, message.message_id)
        status = '<b>✅ Ответ доставлен пользователю.</b>'
    except TelegramBadRequest:
        status = '❌ <b>Не удалось доставить ответ</b> — возможно, пользователь заблокировал бота.'
    await message.reply(status)


@dp.message()
async def forward_to_admin(message: types.Message) -> None:
    topic = await db.get_topic(message.from_user.id)
    if not topic:
        return

    user = message.from_user
    name = user.full_name or 'Аноним'
    username = f'@{user.username}' if user.username else 'без юзернейма'
    header = (
        f'🔔 <b>Обращение в поддержку</b>\n'
        f'👤 <b>{name}</b> ({username})\n'
        f'🆔 <code>{user.id}</code>\n'
        f'📌 Тема: {SUPPORT_TOPICS[topic]}'
    )

    delivered = []
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, header)
            forwarded = await bot.forward_message(admin_id, message.chat.id, message.message_id)
            await db.add_forward(admin_id, forwarded.message_id, message.from_user.id)
            delivered.append(admin_id)
        except TelegramBadRequest:
            logging.exception('Не удалось переслать сообщение админу %s', admin_id)

    if not delivered:
        await message.answer('<b>⚠️ Не удалось доставить сообщение, попробуй позже.</b>')
        return

    await message.answer(f'<b>✅ Сообщение отправлено администраторам.</b>\n📌 Тема: {SUPPORT_TOPICS[topic]}')


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('bot.log', encoding='utf-8'),
        ],
    )

    lock = acquire_lock(LOCK_FILE)
    if lock is None:
        logging.error('Бот уже запущен (блокировка %s занята), завершаюсь.', LOCK_FILE)
        raise SystemExit(2)

    try:
        await db.connect()
        await dp.start_polling(bot)
    finally:
        await db.shutdown()
        lock.close()


if __name__ == '__main__':
    asyncio.run(main())
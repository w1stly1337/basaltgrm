import asyncio
import functools
import glob
import json
import logging
import os
import random
import shutil
import time

import aiofiles
import json_repair

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.command import Command

TOKEN = os.environ.get('BOT_TOKEN', '8814729828:AAEIDJwTUEX8pOS_jevPhIM9TbURZLayzss')
DATA_FILE = 'numbers.json'
BACKUP_FILE = 'numbers.json.bak'
LOCK_FILE = 'bot.lock'
SUPPORT_FILE = 'support.json'
PAYMENTS_FILE = 'payments.jsonl'

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

MAIN_TEXT = (
    '<b>👋 Привет! Это бот для авторизации в BasaltGram.</b>\n\n'
    'ℹ️ Здесь ты можешь приобрести анонимный номер за звезды, либо взять бесплатный номер. '
    'Все коды, отправленные в BasaltGram на приобретенные тобой номера, будут отображаться здесь.\n\n'
    '<b>🕹 Используй кнопки ниже для управления ботом.</b>'
)
SUB_TEXT = '<b>🕹 Подпишись на следующие каналы для продолжения работы с ботом.</b>'

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
dp = Dispatcher()


class NumberStore:
    def __init__(self, path: str = DATA_FILE, backup: str = BACKUP_FILE) -> None:
        self._path = path
        self._backup = backup
        self._users: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        self._cleanup_temp()

        data = await self._read_valid(self._path)
        if data is not None:
            self._users = self._sanitize(data)
            return

        self._quarantine(self._path)

        data = await self._read_valid(self._backup)
        if data is not None:
            logging.warning('Основной файл повреждён, восстанавливаю из резервной копии')
            self._users = self._sanitize(data)
            await self.save()
            return

        data = await self._heal_last_resort()
        if data is not None:
            logging.warning('Основной файл повреждён, восстановлено через json_repair')
            self._users = self._sanitize(data)
            await self.save()
            return

        logging.error('Данные не удалось восстановить, стартую с пустой базой')
        self._users = {}

    @staticmethod
    def _sanitize(data: dict) -> dict[str, list[str]]:
        users: dict[str, list[str]] = {}
        for key, numbers in data.items():
            if not isinstance(numbers, list):
                logging.warning('Пропускаю повреждённую запись пользователя %s (не список)', key)
                continue
            valid = [str(n) for n in numbers if isinstance(n, (str, int)) and str(n)]
            if valid:
                users[str(key)] = valid
        return users

    async def save(self) -> None:
        async with self._lock:
            self._save_now()

    async def get_user(self, uid: int) -> list[str]:
        async with self._lock:
            return list(self._users.get(str(uid)) or [])

    async def ensure_user(self, uid: int) -> None:
        async with self._lock:
            self._users.setdefault(str(uid), [])
            self._save_now()

    async def has_free_number(self, uid: int) -> bool:
        return any(n.startswith(FREE_PREFIX) for n in await self.get_user(uid))

    async def add_number(self, uid: int, prefix: str, low: int, high: int) -> str:
        async with self._lock:
            key = str(uid)
            taken = {n for values in self._users.values() if isinstance(values, list) for n in values}

            existing = self._users.get(key)
            if not isinstance(existing, list):
                numbers = self._users[key] = []
            else:
                numbers = existing

            for _ in range(MAX_GEN_ATTEMPTS):
                number = f'{prefix}{random.randint(low, high)}'
                if number not in taken:
                    numbers.append(number)
                    self._save_now()
                    return number

            raise RuntimeError('Не удалось сгенерировать уникальный номер')

    async def get_all_users(self) -> dict[str, list[str]]:
        async with self._lock:
            return {str(k): list(v) for k, v in self._users.items()}

    def _cleanup_temp(self) -> None:
        for leftover in glob.glob(self._path + '.*.tmp') + glob.glob(self._path + '.tmp'):
            try:
                os.remove(leftover)
            except OSError:
                pass

    @staticmethod
    def _write_durable(path: str, text: str) -> None:
        with open(path, 'w', encoding='utf-8') as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())

    def _save_now(self) -> bool:
        text = json.dumps(self._users, ensure_ascii=False)
        tmp = self._path + '.tmp'

        try:
            self._write_durable(tmp, text)
            shutil.copyfile(tmp, self._backup)
        except OSError:
            logging.exception('Запись временного файла не удалась, база не тронута')
            self._safe_unlink(tmp)
            return False

        try:
            os.replace(tmp, self._path)
        except OSError:
            logging.exception('Замена основного файла не удалась')
            self._safe_unlink(tmp)
            return False

        return True

    def _quarantine(self, path: str) -> None:
        try:
            dst = f'{path}.corrupt-{time.strftime("%Y%m%d-%H%M%S")}'
            os.replace(path, dst)
            logging.error('Повреждённый файл изолирован в %s (данные не удалены)', dst)
        except OSError:
            logging.exception('Не удалось изолировать повреждённый файл')

    async def _read_valid(self, path: str) -> dict[str, list[str]] | None:
        try:
            async with aiofiles.open(path, 'r', encoding='utf-8') as file:
                raw = await file.read()
        except FileNotFoundError:
            return None
        except OSError:
            logging.exception('Не удалось прочитать %s', path)
            return None

        if not raw.strip():
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logging.error('%s повреждён (невалидный JSON)', path)
            return None

        return data if isinstance(data, dict) else None

    async def _heal_last_resort(self) -> dict[str, list[str]] | None:
        pattern = self._path + '.corrupt-*'
        candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        for path in candidates[:3]:
            try:
                async with aiofiles.open(path, 'r', encoding='utf-8') as file:
                    raw = await file.read()
                data = json_repair.loads(raw)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return None

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass


store = NumberStore()


class SupportStore:
    MAX_MAP_SIZE = 10000

    def __init__(self, path: str = SUPPORT_FILE) -> None:
        self._path = path
        self._topics: dict[str, str] = {}
        self._uid_by_fwd: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        self._cleanup_temp()
        try:
            async with aiofiles.open(self._path, 'r', encoding='utf-8') as file:
                raw = await file.read()
        except FileNotFoundError:
            raw = ''
        except OSError:
            logging.exception('Не удалось прочитать %s', self._path)
            raw = ''

        try:
            data = json.loads(raw or '{}')
        except json.JSONDecodeError:
            logging.exception('%s повреждён, стартую с пустой базой', self._path)
            data = {}

        if not isinstance(data, dict):
            return

        topics = data.get('topics')
        forward_map = data.get('map')
        if isinstance(topics, dict):
            self._topics = {str(k): str(v) for k, v in topics.items() if isinstance(v, str)}
        if isinstance(forward_map, dict):
            self._uid_by_fwd = {
                str(k): int(v) for k, v in forward_map.items() if isinstance(v, int) or str(v).isdigit()
            }

    async def _save(self) -> None:
        payload = {
            'topics': self._topics,
            'map': {str(k): v for k, v in self._uid_by_fwd.items()},
        }
        text = json.dumps(payload, ensure_ascii=False)
        tmp = self._path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self._path)
        except OSError:
            logging.exception('Не удалось сохранить %s', self._path)
            self._safe_unlink(tmp)

    async def get_topic(self, uid: int) -> str | None:
        async with self._lock:
            return self._topics.get(str(uid))

    async def set_topic(self, uid: int, topic: str) -> None:
        async with self._lock:
            self._topics[str(uid)] = topic
            await self._save()

    async def close(self, uid: int) -> None:
        async with self._lock:
            self._topics.pop(str(uid), None)
            await self._save()

    async def open_count(self) -> int:
        async with self._lock:
            return len(self._topics)

    async def add_forward(self, admin_id: int, message_id: int, uid: int) -> None:
        async with self._lock:
            self._uid_by_fwd[f'{admin_id}:{message_id}'] = uid
            if len(self._uid_by_fwd) > self.MAX_MAP_SIZE:
                for old in list(self._uid_by_fwd)[:len(self._uid_by_fwd) - self.MAX_MAP_SIZE]:
                    self._uid_by_fwd.pop(old, None)
            await self._save()

    async def pop_uid(self, admin_id: int, message_id: int) -> int | None:
        async with self._lock:
            uid = self._uid_by_fwd.pop(f'{admin_id}:{message_id}', None)
            await self._save()
            return uid

    def _cleanup_temp(self) -> None:
        for leftover in glob.glob(self._path + '.tmp'):
            try:
                os.remove(leftover)
            except OSError:
                pass

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass


support_store = SupportStore()


def log_payment(uid: int, username: str | None, payload: str, amount: int) -> None:
    record = {
        'ts': time.time(),
        'uid': uid,
        'username': username or '',
        'payload': payload,
        'amount': amount,
    }
    try:
        with open(PAYMENTS_FILE, 'a', encoding='utf-8') as file:
            file.write(json.dumps(record, ensure_ascii=False) + '\n')
            file.flush()
            os.fsync(file.fileno())
    except OSError:
        logging.exception('Не удалось записать платёж в %s', PAYMENTS_FILE)


def payment_total() -> int:
    total = 0
    try:
        with open(PAYMENTS_FILE, 'r', encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    total += int(json.loads(line).get('amount') or 0)
                except (json.JSONDecodeError, ValueError):
                    continue
    except FileNotFoundError:
        pass
    return total


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
    logging.exception('Ошибка при обработке update: %s', exc)
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f'<b>⚠️ Ошибка бота:</b>\n<code>{exc.__class__.__name__}: {exc}</code>')
        except TelegramBadRequest:
            pass

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
    await store.ensure_user(query.from_user.id)

    if await store.has_free_number(query.from_user.id):
        await query.message.answer('<b>❌ У вас уже есть бесплатный номер!</b>')
        return

    number = await store.add_number(query.from_user.id, *NUM_FORMATS['free'])
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
    numbers = await store.get_user(query.from_user.id)
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

    await store.ensure_user(uid)

    if payload not in NUM_FORMATS:
        await message.answer('<b>⚠️ Неизвестная оплата, обратитесь в поддержку.</b>')
        return

    number = await store.add_number(uid, *NUM_FORMATS[payload])
    log_payment(uid, message.from_user.username, payload, message.successful_payment.total_amount)
    label = 'анонимный номер' if payload == 'anon' else 'короткий анонимный номер'
    await message.answer(f'<b>✅ Успешно добавлен {label}!\n\n📱 Номер:</b>\n<pre>+{number}</pre>')


@dp.message(Command('stats'))
async def admin_stats(message: types.Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return

    users = await store.get_all_users()
    free = anon = short = 0
    for numbers in users.values():
        for number in numbers:
            if number.startswith(FREE_PREFIX):
                free += 1
            elif len(number) <= 7:
                short += 1
            else:
                anon += 1

    open_tickets = await support_store.open_count()
    paid = payment_total()

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
    for uid_str in await store.get_all_users():
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
    await support_store.set_topic(query.from_user.id, topic)

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
    await support_store.close(query.from_user.id)
    await query.message.edit_text('<b>✅ Обращение закрыто.</b>')


@dp.message(F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def admin_reply_to_support(message: types.Message) -> None:
    uid = await support_store.pop_uid(message.chat.id, message.reply_to_message.message_id)
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
    topic = await support_store.get_topic(message.from_user.id)
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
            await support_store.add_forward(admin_id, forwarded.message_id, message.from_user.id)
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
        await store.load()
        await support_store.load()
        await dp.start_polling(bot)
    finally:
        lock.close()


if __name__ == '__main__':
    asyncio.run(main())
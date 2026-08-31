import sqlite3
import asyncio
import logging
import random

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters.command import Command

bot = Bot(token='8814729828:AAEIDJwTUEX8pOS_jevPhIM9TbURZLayzss')
dp = Dispatcher()

DB_PATH = 'bot.db'
FREE_PREFIX = '7'
ANON_PREFIX = '888'

channels = [('BASALTGRAM News', 'http://t.me/Basaltgram')]


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # Создаём таблицу, если базы ещё нет.
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
            uid TEXT PRIMARY KEY,
            numbers TEXT DEFAULT '[]'
        )''')

        # Если bot.db уже существовала со старой схемой без `numbers`,
        # добавляем недостающий столбец, не удаляя существующих пользователей.
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }

        if 'numbers' not in columns:
            conn.execute(
                "ALTER TABLE users ADD COLUMN numbers TEXT DEFAULT '[]'"
            )

        conn.commit()


def get_numbers(uid: int) -> list:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT numbers FROM users WHERE uid = ?', (str(uid),)).fetchone()
        if row:
            import json
            return json.loads(row[0])
        return []


def save_numbers(uid: int, numbers: list):
    import json
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR REPLACE INTO users (uid, numbers) VALUES (?, ?)',
                      (str(uid), json.dumps(numbers)))
        conn.commit()


def create_user(uid: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT OR IGNORE INTO users (uid, numbers) VALUES (?, ?)',
                      (str(uid), '[]'))
        conn.commit()


def has_free_number(uid: int) -> bool:
    for number in get_numbers(uid):
        if number.startswith(FREE_PREFIX):
            return True
    return False


def is_number_exists(phone: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT uid FROM users').fetchall()
        import json
        for (uid,) in row:
            numbers = json.loads(conn.execute('SELECT numbers FROM users WHERE uid = ?', (uid,)).fetchone()[0])
            if phone in numbers:
                return True
        return False


def add_free_number(uid: int) -> str:
    numbers = get_numbers(uid)
    phone = f'{FREE_PREFIX}{random.randint(111_111_1111, 999_999_9999)}'
    if is_number_exists(phone):
        return add_free_number(uid)
    numbers.append(phone)
    save_numbers(uid, numbers)
    return phone


def add_anon_number(uid: int) -> str:
    numbers = get_numbers(uid)
    phone = f'{ANON_PREFIX}{random.randint(1111_1111, 9999_9999)}'
    if is_number_exists(phone):
        return add_anon_number(uid)
    numbers.append(phone)
    save_numbers(uid, numbers)
    return phone


def add_short_anon(uid: int) -> str:
    numbers = get_numbers(uid)
    phone = f'{ANON_PREFIX}{random.randint(1111, 9999)}'
    if is_number_exists(phone):
        return add_short_anon(uid)
    numbers.append(phone)
    save_numbers(uid, numbers)
    return phone


@dp.message(Command('start'))
async def start(message: types.Message) -> None:
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[])
    for channel in channels:
        member = await bot.get_chat_member(channel[1].replace('http://t.me/', '@'), message.from_user.id)
        if member.status == 'left':
            keyboard.inline_keyboard.append([types.InlineKeyboardButton(text=f'📢 {channel[0]}', url=channel[1])])

    if any(keyboard.inline_keyboard):
        keyboard.inline_keyboard.append([types.InlineKeyboardButton(text='✅ Я подписался', url='http://t.me/BASALTGRAMauthbot?start=1')])
        await message.answer('<b>🕹 Подпишись на следующие каналы для продолжения работы с ботом.</b>', parse_mode='HTML', reply_markup=keyboard)
        return

    keyboard = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='📱 Получить бесплатный номер', callback_data='freenum')],
            [types.InlineKeyboardButton(text='📱 Купить анонимный номер', callback_data='anon')],
            [types.InlineKeyboardButton(text='📱 Купить короткий анонимный номер', callback_data='shortanon')],
            [types.InlineKeyboardButton(text='📊 Мои номера', callback_data='mynums')]
        ]
    )

    await message.answer('<b>👋 Привет! Это бот для авторизации в BASALTGRAM.</b>\n\nℹ️ Здесь ты можешь приобрести анонимный номер за звезды, либо взять бесплатный номер. Все коды, отправленные в BASALTGRAM на приобретенные тобой номера, будут отображаться здесь.\n\n<b>🕹 Используй кнопки ниже для управления ботом.</b>', parse_mode='HTML', reply_markup=keyboard)


@dp.callback_query(F.data == 'freenum')
async def freenum(query: types.CallbackQuery) -> None:
    uid = query.from_user.id
    create_user(uid)

    if has_free_number(uid):
        await query.message.answer('<b>❌ У вас уже есть бесплатный номер!</b>', parse_mode='HTML')
        await query.answer()
        return

    number = add_free_number(uid)
    await query.message.answer(f'<b>✅ Успешно добавлен бесплатный номер!\n\n📱 Номер:</b>\n<pre>+{number}</pre>', parse_mode='HTML')
    await query.answer()


@dp.callback_query(F.data == 'anon')
async def anon(query: types.CallbackQuery) -> None:
    await query.message.answer_invoice(
        title='📱 Анонимный номер\n',
        description='ℹ️ Анонимный номер для входа в BASALTGRAM. Пример: +888 1234 5678',
        prices=[types.LabeledPrice(label='Оплата', amount=100)],
        payload='anon',
        currency='XTR'
    )
    await query.answer()


@dp.callback_query(F.data == 'shortanon')
async def shortanon(query: types.CallbackQuery) -> None:
    await query.message.answer_invoice(
        title='📱 Короткий анонимный номер',
        description='ℹ️ Короткий анонимный номер для входа в BASALTGRAM. Пример: +888 1234',
        prices=[types.LabeledPrice(label='Оплата', amount=200)],
        payload='shortanon',
        currency='XTR'
    )
    await query.answer()


@dp.callback_query(F.data == 'mynums')
async def mynums(query: types.CallbackQuery) -> None:
    numbers = get_numbers(query.from_user.id)
    if not numbers:
        await query.message.answer('<b>❌ У вас нет номеров.</b>', parse_mode='HTML')
        await query.answer()
        return

    numbers_list = []
    for i, number in enumerate(numbers):
        numbers_list.append(f'{i + 1}. <code>+{number}</code>')

    await query.message.answer(f'<b>📱 Список ваших номеров</b>\n\n' + '\n'.join(numbers_list), parse_mode='HTML')
    await query.answer()


@dp.pre_checkout_query(F.invoice_payload == 'anon')
async def anon_payment(query: types.PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@dp.pre_checkout_query(F.invoice_payload == 'shortanon')
async def shortanon_payment(query: types.PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message) -> None:
    payload = message.successful_payment.invoice_payload
    uid = message.from_user.id
    create_user(uid)

    if payload == 'anon':
        number = add_anon_number(uid)
        await message.answer(f'<b>✅ Успешно добавлен анонимный номер!\n\n📱 Номер:</b>\n<pre>+{number}</pre>', parse_mode='HTML')
    elif payload == 'shortanon':
        number = add_short_anon(uid)
        await message.answer(f'<b>✅ Успешно добавлен короткий анонимный номер!\n\n📱 Номер:</b>\n<pre>+{number}</pre>', parse_mode='HTML')


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())

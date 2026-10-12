import asyncio
import os
import json
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery, LabeledPrice, WebAppInfo, KeyboardButton,
    ReplyKeyboardMarkup
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import database as db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
WEBAPP_URL = os.getenv("WEBAPP_URL")
MONO_CARD = os.getenv("MONO_CARD")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class AdminStates(StatesGroup):
    waiting_cheat_name = State()
    waiting_cheat_game = State()
    waiting_cheat_device = State()
    waiting_cheat_description = State()
    waiting_cheat_price_rub = State()
    waiting_cheat_price_stars = State()
    waiting_key_cheat_id = State()
    waiting_key_value = State()

class OrderStates(StatesGroup):
    waiting_receipt = State()

def pixel_header():
    return (
        "██████████████████████\n"
        "█   🎮 PIXEL KAY 🎮   █\n"
        "██████████████████████"
    )

def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(
            text="🎮 Открыть PIXEL KAY",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )]
    ], resize_keyboard=True)

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить чит", callback_data="admin_add_cheat")],
        [InlineKeyboardButton(text="🔑 Добавить ключ", callback_data="admin_add_key")],
        [InlineKeyboardButton(text="📋 Список читов", callback_data="admin_list_cheats")],
        [InlineKeyboardButton(text="🗑 Удалить чит", callback_data="admin_delete_cheat")],
    ])

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await db.init_db()
    text = (
        f"{pixel_header()}\n\n"
        "░░░░░░░░░░░░░░░░░░░░░░\n"
        "▸ OXIDE PUBG\n"
        "▸ DELTA FORCE\n"
        "▸ BRAWL STARS\n"
        "░░░░░░░░░░░░░░░░░░░░░░\n\n"
        "👾 Лучшие читы по лучшим ценам\n"
        "💳 Оплата: Monobank / ⭐ Stars\n\n"
        "⬇️ Нажми кнопку чтобы открыть магазин"
    )
    await message.answer(text, reply_markup=main_keyboard())

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    text = (
        f"{pixel_header()}\n\n"
        "👑 АДМИН ПАНЕЛЬ\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(text, reply_markup=admin_keyboard())

@dp.callback_query(F.data == "admin_add_cheat")
async def admin_add_cheat(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("📝 Введи название чита:")
    await state.set_state(AdminStates.waiting_cheat_name)
    await callback.answer()

@dp.message(AdminStates.waiting_cheat_name)
async def process_cheat_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("🎮 Введи название игры:")
    await state.set_state(AdminStates.waiting_cheat_game)

@dp.message(AdminStates.waiting_cheat_game)
async def process_cheat_game(message: Message, state: FSMContext):
    await state.update_data(game=message.text)
    await message.answer("📱 Введи устройство (Android / iOS / PC):")
    await state.set_state(AdminStates.waiting_cheat_device)

@dp.message(AdminStates.waiting_cheat_device)
async def process_cheat_device(message: Message, state: FSMContext):
    await state.update_data(device=message.text)
    await message.answer("📄 Введи описание чита:")
    await state.set_state(AdminStates.waiting_cheat_description)

@dp.message(AdminStates.waiting_cheat_description)
async def process_cheat_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await message.answer("💵 Введи цену в гривнах (UAH):")
    await state.set_state(AdminStates.waiting_cheat_price_rub)

@dp.message(AdminStates.waiting_cheat_price_rub)
async def process_cheat_price_rub(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введи число!")
        return
    await state.update_data(price_rub=int(message.text))
    await message.answer("⭐ Введи цену в Telegram Stars:")
    await state.set_state(AdminStates.waiting_cheat_price_stars)

@dp.message(AdminStates.waiting_cheat_price_stars)
async def process_cheat_price_stars(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введи число!")
        return
    data = await state.get_data()
    cheat_id = await db.add_cheat(
        data['name'], data['game'], data['device'],
        data['description'], data['price_rub'], int(message.text)
    )
    await message.answer(
        f"✅ Чит добавлен!\n"
        f"🆔 ID: {cheat_id}\n"
        f"📦 {data['name']} | {data['game']} | {data['device']}\n"
        f"💵 {data['price_rub']} UAH | ⭐ {message.text} Stars"
    )
    await state.clear()

@dp.callback_query(F.data == "admin_add_key")
async def admin_add_key(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    cheats = await db.get_cheats()
    if not cheats:
        await callback.message.answer("❌ Сначала добавь читы!")
        await callback.answer()
        return
    text = "🔑 Введи ID чита:\n\n"
    for c in cheats:
        text += f"ID {c['id']} — {c['name']} ({c['game']}, {c['device']})\n"
    await callback.message.answer(text)
    await state.set_state(AdminStates.waiting_key_cheat_id)
    await callback.answer()

@dp.message(AdminStates.waiting_key_cheat_id)
async def process_key_cheat_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введи число!")
        return
    await state.update_data(cheat_id=int(message.text))
    await message.answer("🔑 Введи ключ:")
    await state.set_state(AdminStates.waiting_key_value)

@dp.message(AdminStates.waiting_key_value)
async def process_key_value(message: Message, state: FSMContext):
    data = await state.get_data()
    await db.add_key(data['cheat_id'], message.text)
    await message.answer(f"✅ Ключ добавлен для чита ID {data['cheat_id']}!")
    await state.clear()

@dp.callback_query(F.data == "admin_list_cheats")
async def admin_list_cheats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cheats = await db.get_cheats()
    if not cheats:
        await callback.message.answer("❌ Читов нет")
        await callback.answer()
        return
    text = f"{pixel_header()}\n\n📋 ВСЕ ЧИТЫ:\n━━━━━━━━━━━━━━━━━━━━\n\n"
    for c in cheats:
        text += (
            f"🆔 ID: {c['id']}\n"
            f"📦 {c['name']}\n"
            f"🎮 {c['game']} | 📱 {c['device']}\n"
            f"💵 {c['price_rub']} UAH | ⭐ {c['price_stars']} Stars\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(F.data == "admin_delete_cheat")
async def admin_delete_cheat(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cheats = await db.get_cheats()
    if not cheats:
        await callback.message.answer("❌ Читов нет")
        await callback.answer()
        return
    buttons = []
    for c in cheats:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {c['name']} ({c['game']})",
            callback_data=f"delete_cheat_{c['id']}"
        )])
    await callback.message.answer(
        "Выбери чит для удаления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_cheat_"))
async def confirm_delete_cheat(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    cheat_id = int(callback.data.split("_")[2])
    await db.delete_cheat(cheat_id)
    await callback.message.answer(f"✅ Чит ID {cheat_id} удалён!")
    await callback.answer()

@dp.message(F.web_app_data)
async def handle_webapp_data(message: Message, state: FSMContext):
    data = json.loads(message.web_app_data.data)
    cheat_id = int(data.get("cheat_id"))
    payment_type = data.get("payment_type")

    cheat = await db.get_cheat(cheat_id)
    if not cheat:
        await message.answer("❌ Чит не найден!")
        return

    if payment_type == "stars":
        prices = [LabeledPrice(label=cheat['name'], amount=cheat['price_stars'])]
        await bot.send_invoice(
            chat_id=message.chat.id,
            title=f"🎮 {cheat['name']}",
            description=f"{cheat['game']} | {cheat['device']}\n{cheat['description']}",
            payload=f"stars_{cheat_id}",
            currency="XTR",
            prices=prices,
        )
    else:
        order_id = await db.create_order(
            message.from_user.id,
            message.from_user.username,
            cheat_id,
            "card"
        )
        await state.update_data(order_id=order_id, cheat_id=cheat_id)
        await state.set_state(OrderStates.waiting_receipt)
        text = (
            f"{pixel_header()}\n\n"
            f"🛒 ЗАКАЗ #{order_id}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 {cheat['name']}\n"
            f"🎮 {cheat['game']} | 📱 {cheat['device']}\n"
            f"💵 Сумма: {cheat['price_rub']} UAH\n\n"
            f"🏦 MONOBANK:\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 Номер карты: `{MONO_CARD}`\n\n"
            f"📸 После оплаты отправь скриншот квитанции сюда\n"
            f"⏳ Подтверждение до 15 минут"
        )
        await message.answer(text, parse_mode="Markdown")

@dp.message(OrderStates.waiting_receipt, F.photo)
async def handle_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    cheat_id = data.get("cheat_id")

    file_id = message.photo[-1].file_id
    await db.update_order_receipt(order_id, file_id)

    cheat = await db.get_cheat(cheat_id)
    username = f"@{message.from_user.username}" if message.from_user.username else f"ID: {message.from_user.id}"

    admin_text = (
        f"{pixel_header()}\n\n"
        f"🔔 НОВЫЙ ЗАКАЗ #{order_id}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Покупатель: {username}\n"
        f"📦 Чит: {cheat['name']}\n"
        f"🎮 {cheat['game']} | 📱 {cheat['device']}\n"
        f"💵 Сумма: {cheat['price_rub']} UAH\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{order_id}")
        ]
    ])
    await bot.send_photo(
        chat_id=ADMIN_ID,
        photo=file_id,
        caption=admin_text,
        reply_markup=confirm_kb
    )
    await message.answer(
        f"{pixel_header()}\n\n"
        f"✅ Квитанция получена!\n"
        f"🛒 Заказ #{order_id}\n\n"
        f"⏳ Ожидай подтверждения...\n"
        f"📲 Ключ придёт сюда автоматически"
    )
    await state.clear()

@dp.message(OrderStates.waiting_receipt)
async def handle_receipt_wrong(message: Message):
    await message.answer("📸 Отправь именно фото квитанции!")

@dp.callback_query(F.data.startswith("confirm_"))
async def confirm_order(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    order_id = int(callback.data.split("_")[1])
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден")
        return

    key_row = await db.get_free_key(order['cheat_id'])
    if not key_row:
        await callback.message.answer(
            f"❌ Нет свободных ключей для заказа #{order_id}!\n"
            f"Добавь ключи через /admin"
        )
        await callback.answer()
        return

    await db.mark_key_used(key_row['id'])
    await db.confirm_order(order_id, key_row['key_value'])

    cheat = await db.get_cheat(order['cheat_id'])
    await bot.send_message(
        chat_id=order['user_id'],
        text=(
            f"{pixel_header()}\n\n"
            f"🎉 ОПЛАТА ПОДТВЕРЖДЕНА!\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛒 Заказ #{order_id}\n"
            f"📦 {cheat['name']}\n"
            f"🎮 {cheat['game']} | 📱 {cheat['device']}\n\n"
            f"🔑 ВАШ КЛЮЧ:\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"`{key_row['key_value']}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✨ Спасибо за покупку в PIXEL KAY!"
        ),
        parse_mode="Markdown"
    )
    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n✅ ПОДТВЕРЖДЕНО | Ключ отправлен"
    )
    await callback.answer("✅ Ключ отправлен!")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    order_id = int(callback.data.split("_")[1])
    order = await db.get_order(order_id)
    if not order:
        await callback.answer("❌ Заказ не найден")
        return

    await db.reject_order(order_id)
    await bot.send_message(
        chat_id=order['user_id'],
        text=(
            f"{pixel_header()}\n\n"
            f"❌ ЗАКАЗ #{order_id} ОТКЛОНЁН\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Если это ошибка — напиши нам"
        )
    )
    await callback.message.edit_caption(
        caption=callback.message.caption + f"\n\n❌ ОТКЛОНЕНО"
    )
    await callback.answer("❌ Заказ отклонён")

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    cheat_id = int(payload.split("_")[1])

    order_id = await db.create_order(
        message.from_user.id,
        message.from_user.username,
        cheat_id,
        "stars"
    )

    key_row = await db.get_free_key(cheat_id)
    cheat = await db.get_cheat(cheat_id)

    if not key_row:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"⚠️ STARS ОПЛАТА — НЕТ КЛЮЧЕЙ!\n"
                f"Заказ #{order_id}\n"
                f"Покупатель: @{message.from_user.username}\n"
                f"Чит: {cheat['name']}"
            )
        )
        await message.answer(
            f"{pixel_header()}\n\n"
            f"⭐ Оплата получена!\n"
            f"⏳ Ключ будет выдан вручную в течение 15 минут"
        )
        return

    await db.mark_key_used(key_row['id'])
    await db.confirm_order(order_id, key_row['key_value'])

    await message.answer(
        f"{pixel_header()}\n\n"
        f"🎉 ОПЛАТА ⭐ STARS ПОЛУЧЕНА!\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 {cheat['name']}\n"
        f"🎮 {cheat['game']} | 📱 {cheat['device']}\n\n"
        f"🔑 ВАШ КЛЮЧ:\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"`{key_row['key_value']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✨ Спасибо за покупку в PIXEL KAY!",
        parse_mode="Markdown"
    )

async def main():
    await db.init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

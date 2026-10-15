import asyncio
import json
import logging
import os
import asyncpg
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery, LabeledPrice, WebAppInfo)
import database as db

load_dotenv()
BOT_TOKEN=os.getenv('BOT_TOKEN','')
ADMIN_ID=int(os.getenv('ADMIN_ID','0') or 0)
WEBAPP_URL=os.getenv('WEBAPP_URL','')
MONO_CARD=os.getenv('MONO_CARD','')
if not BOT_TOKEN:
    raise RuntimeError('BOT_TOKEN is required')
bot=Bot(token=BOT_TOKEN)
dp=Dispatcher(storage=MemoryStorage())

class OrderStates(StatesGroup):
    waiting_receipt=State()


def pixel_header():
    return '🎮 PIXEL KEY'


def main_keyboard():
    # An inline Web App button supplies Telegram-signed initData needed by profile/admin API.
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='🎮 Открыть PIXEL KAY',web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


def open_app_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть PIXEL KAY',web_app=WebAppInfo(url=WEBAPP_URL))]])


def admin_order_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='✅ Подтвердить',callback_data=f'confirm_{order_id}'),
        InlineKeyboardButton(text='❌ Отклонить',callback_data=f'reject_{order_id}')
    ]])


def user_label(user):
    return '@'+user.username if user.username else f'ID {user.id} (без username)'


@dp.message(Command('start'))
async def start(message: Message):
    referrer=None
    parts=(message.text or '').split(maxsplit=1)
    if len(parts)>1 and parts[1].startswith('ref_'):
        try: referrer=int(parts[1][4:])
        except ValueError: referrer=None
    try:
        await db.upsert_user(message.from_user.id,message.from_user.username,message.from_user.first_name,referrer)
    except Exception:
        logging.exception('upsert_user failed')
    await message.answer('🎮 PIXEL KAY\nОткрой мини-приложение: каталог, профиль и инвентарь.',reply_markup=main_keyboard())


@dp.message(Command('admin'))
async def admin(message: Message):
    if message.from_user.id!=ADMIN_ID:
        await message.answer('⛔ Нет доступа')
        return
    await message.answer('👑 Панель управления находится в мини-приложении.',reply_markup=open_app_keyboard())


def order_caption(o):
    uname=('@'+o['username']) if o['username'] else f"ID {o['user_id']} (без username)"
    amount=o['amount_uah'] or o['price_rub']
    return (f"🧾 КВИТАНЦИЯ · заказ #{o['id']}\n👤 Покупатель: {uname}\n🆔 Telegram ID: {o['user_id']}\n"
            f"📦 {o['name']} · {o['duration_days']} дн. × {o['quantity'] or 1}\n💵 {amount} UAH\n📝 Комментарий к переводу: {uname}")


@dp.message(Command('orders'))
async def orders_cmd(message: Message):
    if message.from_user.id!=ADMIN_ID:
        await message.answer('⛔ Нет доступа'); return
    rows=[o for o in await db.list_orders(pending_only=True) if o['payment_type']=='card']
    ready=[o for o in rows if o['receipt_file_id']]
    waiting=len(rows)-len(ready)
    if not ready:
        await message.answer('✅ Нет квитанций на проверке.'+(f'\n⏳ Ждут квитанцию от покупателя: {waiting}' if waiting else '')); return
    await message.answer(f'🧾 На проверке: {len(ready)}'+(f' · ждут квитанцию: {waiting}' if waiting else ''))
    for o in ready[:20]:
        try:
            await bot.send_photo(ADMIN_ID,o['receipt_file_id'],caption=order_caption(o),reply_markup=admin_order_keyboard(o['id']))
        except Exception:
            await bot.send_message(ADMIN_ID,order_caption(o),reply_markup=admin_order_keyboard(o['id']))


async def notify_key(order, key_value=None):
    # Keys are shown only in the in-app profile; send a short status notice in chat.
    await bot.send_message(
        order['user_id'],
        f"✅ Оплата заказа #{order['id']} прошла проверку.\n"
        "Обнови страницу мини-приложения PIXEL KAY и посмотри результат в профиле."
    )


async def approve_and_notify(order_id):
    result=await db.approve_order(order_id)
    if not result['ok']:
        return result
    if result.get('already'):
        return result
    await notify_key(result['order'],result['key'])
    return result


@dp.message(F.web_app_data)
async def legacy_webapp_data(message: Message, state: FSMContext):
    """Compatibility for the existing Mini App sendData checkout."""
    try:
        data=json.loads(message.web_app_data.data)
        product_id=int(data.get('product_id',data.get('cheat_id')))
        duration=int(data.get('duration_days',30))
        payment_type=data.get('payment_type','card')
    except Exception:
        await message.answer('Не удалось прочитать заказ. Открой магазин и попробуй снова.')
        return
    c=await db.get_cheat(product_id)
    if not c or not c['is_active']:
        await message.answer('Товар не найден или снят с продажи.')
        return
    if payment_type=='stars':
        order_id=await db.create_order(message.from_user.id,message.from_user.username,product_id,'stars',duration)
        await bot.send_invoice(chat_id=message.chat.id,title=c['name'][:32],
            description=f"{c['game']} · {duration} дн.",payload=f'order_{order_id}',
            currency='XTR',prices=[LabeledPrice(label=f"{c['name']} · {duration} дн.",amount=int(c['price_stars']))])
        return
    order_id=await db.create_order(message.from_user.id,message.from_user.username,product_id,'card',duration)
    await state.update_data(order_id=order_id,cheat_id=product_id)
    await state.set_state(OrderStates.waiting_receipt)
    username=user_label(message.from_user)
    await message.answer(
        f"🛒 Заказ #{order_id}\n📦 {c['name']} · {duration} дн.\nСумма: {c['price_rub']} UAH\n\n"
        f"Карта: `{MONO_CARD}`\nВ комментарии к переводу укажи {username}.\nПосле оплаты отправь сюда фото квитанции.",parse_mode='Markdown')


@dp.message(OrderStates.waiting_receipt,F.photo)
async def chat_receipt(message: Message,state: FSMContext):
    data=await state.get_data(); order_id=data.get('order_id'); product_id=data.get('cheat_id')
    if not order_id:
        await message.answer('Заказ не найден. Открой магазин и начни покупку заново.')
        await state.clear(); return
    file_id=message.photo[-1].file_id
    await db.update_order_receipt(order_id,file_id)
    order=await db.get_order_details(order_id)
    caption=order_caption(order) if order else f'🧾 КВИТАНЦИЯ · заказ #{order_id}\n👤 {user_label(message.from_user)}'
    await bot.send_photo(ADMIN_ID,file_id,caption=caption,reply_markup=admin_order_keyboard(order_id))
    await state.clear()


@dp.message(OrderStates.waiting_receipt)
async def chat_receipt_wrong(message: Message):
    await message.answer('Пришли фото квитанции.')


@dp.callback_query(F.data.startswith('confirm_'))
async def confirm_callback(callback: CallbackQuery):
    if callback.from_user.id!=ADMIN_ID:
        await callback.answer('Нет доступа',show_alert=True); return
    try: order_id=int(callback.data.split('_',1)[1])
    except ValueError:
        await callback.answer('Неверный номер заказа',show_alert=True); return
    result=await approve_and_notify(order_id)
    if not result['ok']:
        text='Заказ не найден' if result['reason']=='not_found' else ('Нет свободного ключа на этот срок. Добавь ключ в панели и повтори.' if result['reason']=='no_key' else 'Заказ уже обработан.')
        await callback.answer(text,show_alert=True); return
    if result.get('already'):
        await callback.answer('Заказ уже подтверждён'); return
    try:
        caption=(callback.message.caption or '')+'\n\n✅ ПОДТВЕРЖДЕНО · ключ добавлен в профиль покупателя'
        if callback.message.photo:
            await callback.message.edit_caption(caption=caption,reply_markup=None)
        else:
            await callback.message.edit_text((callback.message.text or '')+'\n\n✅ ПОДТВЕРЖДЕНО · ключ добавлен в профиль покупателя',reply_markup=None)
    except Exception: pass
    await callback.answer('Оплата подтверждена, ключ в профиле покупателя')


@dp.callback_query(F.data.startswith('reject_'))
async def reject_callback(callback: CallbackQuery):
    if callback.from_user.id!=ADMIN_ID:
        await callback.answer('Нет доступа',show_alert=True); return
    try: order_id=int(callback.data.split('_',1)[1])
    except ValueError:
        await callback.answer('Неверный номер заказа',show_alert=True); return
    order=await db.get_order(order_id)
    if not order:
        await callback.answer('Заказ не найден',show_alert=True); return
    if order['status']!='pending':
        await callback.answer('Заказ уже обработан',show_alert=True); return
    await db.reject_order(order_id)
    await bot.send_message(order['user_id'],f'❌ Заказ #{order_id} отклонён. Если это ошибка, напиши в поддержку.')
    try:
        caption=(callback.message.caption or '')+'\n\n❌ ОТКЛОНЕНО'
        if callback.message.photo: await callback.message.edit_caption(caption=caption,reply_markup=None)
        else: await callback.message.edit_text((callback.message.text or '')+'\n\n❌ ОТКЛОНЕНО',reply_markup=None)
    except Exception: pass
    await callback.answer('Заказ отклонён')


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    try:
        payload=query.invoice_payload
        if not payload.startswith('order_'):
            await query.answer(ok=False,error_message='Обнови приложение и создай заказ заново.'); return
        order_id=int(payload.split('_',1)[1])
        order=await db.get_order_details(order_id)
        if not order or int(order['user_id'])!=int(query.from_user.id) or order['status']!='pending' or order['payment_type']!='stars':
            await query.answer(ok=False,error_message='Заказ не найден или уже обработан.'); return
        if int(query.total_amount)!=int(order['amount_stars'] or order['price_stars']):
            await query.answer(ok=False,error_message='Сумма счёта изменилась. Создай заказ заново.'); return
        stock=await db.keys_count(order['cheat_id'])
        if int(stock.get(int(order['duration_days']),0))<int(order['quantity'] or 1):
            await query.answer(ok=False,error_message='Нет ключа на этот срок. Выбери другой товар или попробуй позже.'); return
        await query.answer(ok=True)
    except Exception:
        await query.answer(ok=False,error_message='Не удалось проверить заказ. Попробуй позже.')


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payment=message.successful_payment
    try:
        if payment.invoice_payload.startswith('order_'):
            order_id=int(payment.invoice_payload.split('_',1)[1])
        else:
            # Legacy invoice payload: stars_<product_id>
            product_id=int(payment.invoice_payload.split('_',1)[1])
            order_id=await db.create_order(message.from_user.id,message.from_user.username,product_id,'stars',30)
        order=await db.get_order(order_id)
        if not order or int(order['user_id'])!=int(message.from_user.id):
            await bot.send_message(ADMIN_ID,f'⚠️ Оплата Stars получена, но заказ #{order_id} не найден/не совпал с покупателем.')
            return
        result=await db.approve_order(order_id,payment.telegram_payment_charge_id)
        if not result['ok']:
            await bot.send_message(ADMIN_ID,f'⚠️ Оплата Stars по заказу #{order_id} получена, но ключ не выдан ({result.get("reason")}). Проверь склад.')
            await bot.send_message(message.from_user.id,f'Оплата получена. Заказ #{order_id} передан администратору: ключ будет выдан после проверки наличия.')
            return
        if not result.get('already'):
            await notify_key(result['order'],result['key'])
    except Exception:
        await bot.send_message(ADMIN_ID,'⚠️ Ошибка обработки successful_payment. Проверь заказы и платёж вручную.')
        raise


LOCK_ID=727002

async def acquire_single_instance_lock():
    """Только один бот на базу: второй экземпляр ждёт, а не вызывает TelegramConflictError."""
    conn=await asyncpg.connect(os.getenv('DATABASE_URL'))
    waited=False
    while not await conn.fetchval('SELECT pg_try_advisory_lock($1)',LOCK_ID):
        if not waited:
            logging.warning('Другой экземпляр бота уже запущен — жду освобождения блокировки...')
            waited=True
        await asyncio.sleep(10)
    logging.info('Бот получил блокировку единственного экземпляра')
    return conn  # соединение держим открытым до конца работы


async def main():
    logging.basicConfig(level=logging.INFO)
    await db.init_db()
    lock_conn=await acquire_single_instance_lock()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        await lock_conn.close()

if __name__=='__main__':
    asyncio.run(main())

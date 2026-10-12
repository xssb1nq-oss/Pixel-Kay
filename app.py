import os
import json
import hmac
import html
import hashlib
import base64
import time
import logging
import asyncio
import threading
import urllib.parse
import urllib.request
from io import BytesIO

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, send_file
import database as db

load_dotenv()
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0') or 0)
MAX_UPLOAD = 8 * 1024 * 1024


def run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def api_call(method, fields=None, files=None):
    """Small Bot API client used by the Flask side for receipts and notices."""
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN is not configured')
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/{method}'
    fields = fields or {}
    if files:
        boundary = '----PixelKay' + os.urandom(12).hex()
        body = bytearray()
        for name, value in fields.items():
            wire_value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{wire_value}\r\n'.encode())
        for name, (filename, content, mime) in files.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode())
            body.extend(content)
            body.extend(b'\r\n')
        body.extend(f'--{boundary}--\r\n'.encode())
        req = urllib.request.Request(url, data=bytes(body), headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    else:
        body = urllib.parse.urlencode({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict,list)) else v for k,v in fields.items()}).encode()
        req = urllib.request.Request(url, data=body, headers={'Content-Type':'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.loads(res.read())
    if not data.get('ok'):
        raise RuntimeError(data.get('description', 'Telegram Bot API error'))
    return data['result']


def verified_user():
    """Validate Telegram WebApp initData and return its signed user object."""
    raw = request.headers.get('X-Telegram-Init-Data', '')
    if not raw and request.is_json:
        raw = (request.get_json(silent=True) or {}).get('initData', '')
    if not raw or not BOT_TOKEN:
        return None
    try:
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        values = {k: v[0] for k,v in parsed.items()}
        received = values.pop('hash', '')
        if not received:
            return None
        data_check = '\n'.join(f'{k}={v}' for k,v in sorted(values.items()))
        secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received, expected):
            return None
        auth_date = int(values.get('auth_date', '0'))
        if auth_date <= 0 or time.time() - auth_date > 86400 or auth_date > time.time() + 60:
            return None
        user = json.loads(values.get('user', '{}'))
        if not user.get('id'):
            return None
        return user
    except Exception:
        return None


def user_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = verified_user()
        if not user:
            return jsonify({'error':'Открой приложение из Telegram и попробуй снова'}), 401
        request.tg_user = user
        return fn(*args, **kwargs)
    return wrapped


def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    @user_required
    def wrapped(*args, **kwargs):
        if int(request.tg_user['id']) != ADMIN_ID:
            return jsonify({'error':'Нет доступа'}), 403
        return fn(*args, **kwargs)
    return wrapped


def json_safe(row):
    from datetime import timezone
    out = dict(row)
    for k,v in list(out.items()):
        if hasattr(v, 'isoformat'):
            if getattr(v, 'tzinfo', None) is None: v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat().replace('+00:00', 'Z')
    return out


def public_product(c, include_admin=False):
    def decode(value, default):
        try: return json.loads(value or '')
        except Exception: return default
    out = {
        'id':c['id'], 'name':c['name'], 'game':c['game'], 'device':c['device'],
        'description':c['description'] or '', 'price_rub':c['price_rub'], 'price_stars':c['price_stars'],
        'category':c['category'] or 'Без категории', 'features':decode(c['features'], []),
        'durations':decode(c['durations'], [30]), 'image_url':f"/api/product/{c['id']}/image" if c['image_data'] else None,
        'is_active':bool(c['is_active'])
    }
    out['stock'] = run_async(db.keys_count(c['id']))
    return out


@app.route('/')
def index():
    return send_from_directory(app.root_path, 'index.html')


@app.route('/api/cheats')
def get_cheats():
    try:
        return jsonify([public_product(c) for c in run_async(db.get_cheats())])
    except Exception:
        logging.exception('get_cheats failed')
        return jsonify({'error':'Не удалось загрузить каталог'}), 500


@app.route('/api/product/<int:product_id>/image')
def product_image(product_id):
    try:
        c = run_async(db.get_cheat(product_id))
        if not c or not c['image_data']:
            return '', 404
        return send_file(BytesIO(bytes(c['image_data'])), mimetype=c['image_mime'] or 'image/jpeg', max_age=0)
    except Exception:
        logging.exception('product_image failed')
        return '', 500


@app.route('/api/profile')
@user_required
def profile():
    user = request.tg_user
    inventory = run_async(db.get_inventory(user['id']))
    orders = run_async(db.list_orders(user_id=user['id']))
    return jsonify({'user':user, 'inventory':[json_safe(x) for x in inventory], 'orders':[json_safe(x) for x in orders], 'is_admin':int(user['id']) == ADMIN_ID})


@app.route('/api/orders', methods=['POST'])
@user_required
def create_order():
    data = request.get_json(silent=True) or {}
    try:
        product_id = int(data.get('product_id', data.get('cheat_id')))
        duration_days = int(data.get('duration_days', 30))
        payment_type = data.get('payment_type', 'card')
    except (TypeError, ValueError):
        return jsonify({'error':'Некорректный товар или срок'}), 400
    if payment_type not in ('card','stars'):
        return jsonify({'error':'Неизвестный способ оплаты'}), 400
    c = run_async(db.get_cheat(product_id))
    if not c or not c['is_active']:
        return jsonify({'error':'Товар больше недоступен'}), 404
    try: durations = json.loads(c['durations'] or '[30]')
    except Exception: durations = [30]
    if duration_days not in [int(x) for x in durations]:
        return jsonify({'error':'Срок больше недоступен'}), 400
    if payment_type == 'card' and not os.getenv('MONO_CARD'):
        return jsonify({'error':'Не настроены реквизиты карты'}), 503
    stock = run_async(db.keys_count(product_id))
    if int(stock.get(duration_days, stock.get(str(duration_days), 0))) < 1:
        return jsonify({'error':'На выбранный срок сейчас нет ключей'}), 409
    user = request.tg_user
    order_id = run_async(db.create_order(user['id'], user.get('username'), product_id, payment_type, duration_days))
    if payment_type == 'stars':
        # This order remains available for chat-based Telegram Stars invoice flow.
        try:
            result = api_call('sendInvoice', {
                'chat_id':user['id'], 'title':c['name'][:32],
                'description':f"{c['game']} · {duration_days} дн. · заказ #{order_id}"[:255],
                'payload':f'order_{order_id}', 'provider_token':'', 'currency':'XTR',
                'prices':[{'label':f"{c['name']} · {duration_days} дн.",'amount':int(c['price_stars'])}]
            })
            return jsonify({'order_id':order_id,'payment_type':'stars','invoice_sent':True})
        except Exception:
            logging.exception('Could not send Stars invoice')
            run_async(db.reject_order(order_id))
            return jsonify({'error':'Не удалось создать счёт Stars'}), 502
    username = '@' + user['username'] if user.get('username') else f"ID {user['id']}"
    return jsonify({
        'order_id':order_id, 'payment_type':'card', 'card':os.getenv('MONO_CARD'),
        'amount_uah':c['price_rub'], 'product_name':c['name'], 'duration_days':duration_days,
        'buyer_username':username,
        'payment_comment':username,
        'instructions':'В комментарии к переводу обязательно укажи свой Telegram @username. Если username нет — укажи Telegram ID.'
    })


@app.route('/api/orders/<int:order_id>/receipt', methods=['POST'])
@user_required
def upload_receipt(order_id):
    user = request.tg_user
    order = run_async(db.get_order_details(order_id))
    if not order or int(order['user_id']) != int(user['id']):
        return jsonify({'error':'Заказ не найден'}), 404
    if order['status'] != 'pending' or order['payment_type'] != 'card':
        return jsonify({'error':'Для этого заказа нельзя загрузить квитанцию'}), 409
    data = request.get_json(silent=True) or {}
    raw = data.get('image_data','')
    if ',' in raw: raw = raw.split(',',1)[1]
    try: content = base64.b64decode(raw, validate=True)
    except Exception: return jsonify({'error':'Не удалось прочитать изображение'}), 400
    mime = data.get('mime_type','image/jpeg')
    if mime not in ('image/jpeg','image/png','image/webp'):
        return jsonify({'error':'Поддерживаются JPG, PNG и WEBP'}), 400
    if not content or len(content) > MAX_UPLOAD:
        return jsonify({'error':'Файл пустой или больше 8 МБ'}), 413
    username = '@'+user['username'] if user.get('username') else f"ID {user['id']} (username не задан)"
    caption = (f"🧾 КВИТАНЦИЯ · заказ #{order_id}\n"
               f"👤 Покупатель: {username}\n"
               f"🆔 Telegram ID: {user['id']}\n"
               f"📦 {order['name']} · {order['duration_days']} дн.\n"
               f"💵 {order['price_rub']} UAH\n"
               f"📝 Комментарий к переводу: {username}")
    markup = {'inline_keyboard':[[
        {'text':'✅ Подтвердить','callback_data':f'confirm_{order_id}'},
        {'text':'❌ Отклонить','callback_data':f'reject_{order_id}'}
    ]]}
    try:
        ext = {'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[mime]
        sent = api_call('sendPhoto', {'chat_id':ADMIN_ID,'caption':caption,'reply_markup':markup},
                        {'photo':(f'receipt_{order_id}.{ext}',content,mime)})
        file_id = sent['photo'][-1]['file_id']
        run_async(db.update_order_receipt(order_id,file_id))
        return jsonify({'ok':True,'message':'Квитанция отправлена на проверку'})
    except Exception:
        logging.exception('receipt forwarding failed')
        return jsonify({'error':'Не удалось отправить квитанцию администратору. Попробуй ещё раз.'}), 502


@app.route('/api/admin/products')
@admin_required
def admin_products():
    return jsonify([public_product(c, True) for c in run_async(db.get_cheats(include_inactive=True))])


@app.route('/api/admin/products', methods=['POST'])
@admin_required
def create_product():
    return save_product()


@app.route('/api/admin/products/<int:product_id>', methods=['PUT'])
@admin_required
def edit_product(product_id):
    if not run_async(db.get_cheat(product_id)): return jsonify({'error':'Товар не найден'}),404
    return save_product(product_id)


def save_product(product_id=None):
    data = request.get_json(silent=True) or {}
    required = ('name','game','device')
    if any(not str(data.get(k,'')).strip() for k in required):
        return jsonify({'error':'Заполни название, игру и устройство'}),400
    try:
        features = data.get('features',[])
        durations = sorted({int(x) for x in data.get('durations',[30]) if int(x)>0})
        if not durations: raise ValueError()
        image_data = None
        mime = None
        image_raw = data.get('image_data')
        if image_raw:
            if ',' in image_raw: image_raw = image_raw.split(',',1)[1]
            image_data = base64.b64decode(image_raw,validate=True)
            mime = data.get('image_mime','image/jpeg')
            if mime not in ('image/jpeg','image/png','image/webp') or len(image_data)>MAX_UPLOAD: raise ValueError()
        payload = {'name':str(data['name'])[:120],'game':str(data['game'])[:100],'device':str(data['device'])[:100],
            'description':str(data.get('description',''))[:5000],'price_rub':int(data.get('price_rub',0)),
            'price_stars':int(data.get('price_stars',0)),'category':str(data.get('category','Без категории') or 'Без категории')[:100],
            'features':[str(x)[:200] for x in features if str(x).strip()],'durations':durations}
        if payload['price_rub']<0 or payload['price_stars']<1: raise ValueError()
        if image_data is not None: payload.update({'image_data':image_data,'image_mime':mime})
        new_id = run_async(db.save_cheat(payload, product_id))
        if not new_id: return jsonify({'error':'Не удалось сохранить товар'}),404
        return jsonify({'ok':True,'id':new_id})
    except Exception as e:
        logging.exception('save_product failed')
        return jsonify({'error':'Проверь цены, сроки и файл фото'}),400


@app.route('/api/admin/products/<int:product_id>', methods=['DELETE'])
@admin_required
def archive_product(product_id):
    run_async(db.delete_cheat(product_id))
    return jsonify({'ok':True,'message':'Товар скрыт из каталога; история заказов сохранена'})


@app.route('/api/admin/products/<int:product_id>/keys', methods=['POST'])
@admin_required
def add_product_keys(product_id):
    data=request.get_json(silent=True) or {}
    cheat=run_async(db.get_cheat(product_id))
    if not cheat: return jsonify({'error':'Товар не найден'}),404
    try:
        duration=int(data.get('duration_days'))
        keys=[x.strip() for x in str(data.get('keys','')).splitlines() if x.strip()]
        allowed=[int(x) for x in json.loads(cheat['durations'] or '[30]')]
        if duration not in allowed or not keys: raise ValueError()
        added=run_async(db.add_keys(product_id,keys,duration))
        return jsonify({'ok':True,'added':added,'skipped':len(keys)-added})
    except Exception:
        logging.exception('add_product_keys failed')
        return jsonify({'error':'Проверь срок и список ключей (по одному на строку)'}),400


@app.route('/api/admin/orders')
@admin_required
def admin_orders():
    rows=run_async(db.list_orders(pending_only=True))
    return jsonify([json_safe(r) for r in rows])


@app.route('/api/admin/orders/<int:order_id>/receipt')
@admin_required
def admin_receipt(order_id):
    order=run_async(db.get_order(order_id))
    if not order or not order['receipt_file_id']: return '',404
    try:
        info=api_call('getFile',{'file_id':order['receipt_file_id']})
        url=f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}"
        with urllib.request.urlopen(url,timeout=30) as r: content=r.read()
        mime='image/jpeg'
        return send_file(BytesIO(content),mimetype=mime,max_age=0)
    except Exception:
        logging.exception('admin_receipt failed')
        return '',502


@app.route('/api/keys_count/<int:cheat_id>')
def keys_count(cheat_id):
    try: return jsonify({'stock':run_async(db.keys_count(cheat_id))})
    except Exception: return jsonify({'stock':{}}),500


def run_bot():
    import bot
    asyncio.run(bot.main())


if __name__ == '__main__':
    bot_thread=threading.Thread(target=run_bot,daemon=True)
    bot_thread.start()
    port=int(os.getenv('PORT',5000))
    app.run(host='0.0.0.0',port=port)

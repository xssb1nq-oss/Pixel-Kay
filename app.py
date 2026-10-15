import os
import json
import re
import random
from datetime import datetime, timezone
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
app.config['MAX_CONTENT_LENGTH'] = 48 * 1024 * 1024
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN_ID = int(os.getenv('ADMIN_ID', '0') or 0)
WEBAPP_URL = os.getenv('WEBAPP_URL', '')
MAX_UPLOAD = 8 * 1024 * 1024


_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True, name='db-loop').start()


def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout=60)


try:
    run_async(db.init_db())
except Exception:
    logging.exception('init_db failed')


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


def viewing_as_buyer():
    return request.headers.get('X-View-As', '').strip().lower() == 'buyer'


_BAN_CACHE = {}


def is_banned(uid):
    uid = int(uid)
    if uid == ADMIN_ID: return False
    hit = _BAN_CACHE.get(uid)
    if hit and time.time() - hit[1] < 30: return hit[0]
    val = bool(run_async(db.is_banned(uid)))
    _BAN_CACHE[uid] = (val, time.time())
    return val


def deny_banned():
    if is_banned(request.tg_user['id']):
        return jsonify({'error':'Аккаунт заблокирован. Напиши в поддержку.'}), 403
    return None


def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    @user_required
    def wrapped(*args, **kwargs):
        if int(request.tg_user['id']) != ADMIN_ID or viewing_as_buyer():
            return jsonify({'error':'Нет доступа'}), 403
        return fn(*args, **kwargs)
    return wrapped


def json_safe(row):
    def conv(v):
        if hasattr(v, 'isoformat'):
            if getattr(v, 'tzinfo', None) is None: v = v.replace(tzinfo=timezone.utc)
            return v.isoformat().replace('+00:00', 'Z')
        if isinstance(v, (bytes, bytearray, memoryview)): return None
        if isinstance(v, dict): return {k: conv(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)): return [conv(x) for x in v]
        return v
    return conv(dict(row))


def decode_json(value, default):
    try: return json.loads(value or '')
    except Exception: return default


def clean_username(user):
    if user.get('username'): return '@' + user['username']
    return user.get('first_name') or 'Покупатель'


def notify_async(chat_ids, text, button=True, delay=0.05):
    """Отправка сообщений в фоне (рассылка / уведомления)."""
    ids = list(chat_ids)
    if not ids: return
    markup = None
    if button and WEBAPP_URL.startswith('https://'):
        markup = {'inline_keyboard': [[{'text': '🎮 Открыть PIXEL KAY', 'web_app': {'url': WEBAPP_URL}}]]}
    def worker():
        sent = 0
        for cid in ids:
            try:
                fields = {'chat_id': cid, 'text': text}
                if markup: fields['reply_markup'] = markup
                api_call('sendMessage', fields)
                sent += 1
            except RuntimeError as e:
                msg = str(e).lower()
                if any(x in msg for x in ('blocked', 'deactivated', 'chat not found')):
                    try: run_async(db.mark_blocked(cid))
                    except Exception: pass
            except Exception:
                logging.exception('notify failed')
            time.sleep(delay)
        logging.info('broadcast done: %s/%s', sent, len(ids))
    threading.Thread(target=worker, daemon=True).start()


RANK_STEPS = [(0, 0), (3, 3), (10, 5), (25, 10)]
QTY_DISCOUNT = {1: 0, 3: 5, 5: 10}


def price_for(c, duration):
    """Цена товара на конкретный срок (если задана отдельно), иначе базовая."""
    pr = (decode_json(c['extra'], {}).get('prices') or {}).get(str(duration))
    if pr:
        try: return int(pr.get('uah', c['price_rub'])), max(1, int(pr.get('stars', c['price_stars'])))
        except (TypeError, ValueError): pass
    return int(c['price_rub']), int(c['price_stars'])


def rank_discount(purchases):
    d = 0
    for need, pct in RANK_STEPS:
        if purchases >= need: d = pct
    return d


def public_product(c, include_admin=False, stock_map=None, shots=None):
    extra = decode_json(c['extra'], {}) if 'extra' in c.keys() else {}
    out = {
        'id':c['id'], 'name':c['name'], 'game':c['game'], 'device':c['device'],
        'description':c['description'] or '', 'price_rub':c['price_rub'], 'price_stars':c['price_stars'],
        'category':c['category'] or 'Без категории', 'features':decode_json(c['features'], []),
        'durations':decode_json(c['durations'], [30]), 'image_url':f"/api/product/{c['id']}/image" if c['image_data'] else None,
        'is_active':bool(c['is_active']),
        'devices':extra.get('devices') or [], 'status':extra.get('status') or 'working', 'risk':extra.get('risk') or None,
        'version':extra.get('version') or '', 'updated_at':extra.get('updated_at') or None,
        'badges':extra.get('badges') or [], 'old_price_rub':extra.get('old_price_rub'),
        'old_price_stars':extra.get('old_price_stars'), 'video_url':extra.get('video_url') or '',
        'trial':bool(extra.get('trial')), 'prices':extra.get('prices') or {},
        'screenshots':[f"/api/product/{c['id']}/shot/{i}" for i in ((shots or {}).get(int(c['id']), []))],
    }
    if include_admin:
        out['instruction'] = extra.get('instruction') or ''
    if stock_map is None:
        out['stock'] = run_async(db.keys_count(c['id']))
    else:
        out['stock'] = stock_map.get(int(c['id']), {})
    return out


def catalog(include_inactive=False):
    rows = run_async(db.get_cheats(include_inactive=include_inactive))
    stock_map = run_async(db.keys_count_all())
    shots = run_async(db.shots_map())
    return [public_product(c, include_inactive, stock_map, shots) for c in rows]


@app.route('/')
def index():
    return send_from_directory(app.root_path, 'index.html')


@app.route('/api/cheats')
def get_cheats():
    try:
        return jsonify(catalog())
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


@app.route('/api/product/<int:product_id>/shot/<int:shot_id>')
def product_shot(product_id, shot_id):
    try:
        row = run_async(db.get_shot(shot_id))
        if not row: return '', 404
        return send_file(BytesIO(bytes(row['data'])), mimetype=row['mime'] or 'image/jpeg', max_age=3600)
    except Exception:
        logging.exception('product_shot failed')
        return '', 500


@app.route('/api/profile')
@user_required
def profile():
    user = request.tg_user
    try: run_async(db.upsert_user(user['id'], user.get('username'), user.get('first_name')))
    except Exception: logging.exception('upsert_user failed')
    inventory = []
    for x in run_async(db.get_inventory(user['id'])):
        d = json_safe(x)
        d['instruction'] = decode_json(d.pop('extra', None), {}).get('instruction', '')
        inventory.append(d)
    orders = run_async(db.list_orders(user_id=user['id']))
    return jsonify({'user':user, 'inventory':inventory, 'orders':[json_safe(x) for x in orders],
                    'is_admin':int(user['id']) == ADMIN_ID and not viewing_as_buyer(),
                    'real_admin':int(user['id']) == ADMIN_ID,
                    'view_as':'buyer' if (int(user['id']) == ADMIN_ID and viewing_as_buyer()) else '',
                    'banned':is_banned(user['id']),
                    'purchases_count':run_async(db.purchases_count(user['id']))})


@app.route('/api/orders', methods=['POST'])
@user_required
def create_order():
    _d = deny_banned()
    if _d: return _d
    data = request.get_json(silent=True) or {}
    try:
        product_id = int(data.get('product_id', data.get('cheat_id')))
        duration_days = int(data.get('duration_days', 30))
        payment_type = data.get('payment_type', 'card')
        quantity = int(data.get('quantity', 1) or 1)
    except (TypeError, ValueError):
        return jsonify({'error':'Некорректный товар или срок'}), 400
    if payment_type not in ('card','stars'):
        return jsonify({'error':'Неизвестный способ оплаты'}), 400
    if quantity not in QTY_DISCOUNT:
        return jsonify({'error':'Количество: 1, 3 или 5'}), 400
    c = run_async(db.get_cheat(product_id))
    if not c or not c['is_active']:
        return jsonify({'error':'Товар больше недоступен'}), 404
    try: durations = json.loads(c['durations'] or '[30]')
    except Exception: durations = [30]
    if duration_days not in [int(x) for x in durations]:
        return jsonify({'error':'Срок больше недоступен'}), 400
    if decode_json(c['extra'], {}).get('status') == 'down':
        return jsonify({'error':'Этот чит сейчас не работает, покупка закрыта'}), 409
    if payment_type == 'card' and not os.getenv('MONO_CARD'):
        return jsonify({'error':'Не настроены реквизиты карты'}), 503
    stock = run_async(db.keys_count(product_id))
    if int(stock.get(duration_days, stock.get(str(duration_days), 0))) < quantity:
        return jsonify({'error':'На выбранный срок недостаточно ключей'}), 409
    user = request.tg_user
    promo = None
    promo_code = str(data.get('promo') or '').strip()
    if promo_code:
        promo = run_async(db.find_promo(promo_code))
        if not promo:
            return jsonify({'error':'Промокод недействителен или закончился'}), 400
    purchases = run_async(db.purchases_count(user['id']))
    _, paid_refs = run_async(db.referral_stats(user['id']))
    pct = min(90, QTY_DISCOUNT[quantity] + rank_discount(purchases) + min(10, paid_refs * 2) + (int(promo['percent']) if promo else 0))
    unit_uah, unit_stars = price_for(c, duration_days)
    amount_uah = round(unit_uah * quantity * (100 - pct) / 100)
    amount_stars = max(1, round(unit_stars * quantity * (100 - pct) / 100))
    order_id = run_async(db.create_order(user['id'], user.get('username'), product_id, payment_type, duration_days,
                                         quantity, amount_uah, amount_stars, promo['code'] if promo else None))
    if payment_type == 'stars':
        try:
            api_call('sendInvoice', {
                'chat_id':user['id'], 'title':c['name'][:32],
                'description':f"{c['game']} · {duration_days} дн. × {quantity} · заказ #{order_id}"[:255],
                'payload':f'order_{order_id}', 'provider_token':'', 'currency':'XTR',
                'prices':[{'label':f"{c['name']} · {duration_days} дн. × {quantity}",'amount':amount_stars}]
            })
            return jsonify({'order_id':order_id,'payment_type':'stars','invoice_sent':True,'amount_stars':amount_stars,'discount_percent':pct})
        except Exception:
            logging.exception('Could not send Stars invoice')
            run_async(db.reject_order(order_id))
            return jsonify({'error':'Не удалось создать счёт Stars'}), 502
    username = '@' + user['username'] if user.get('username') else f"ID {user['id']}"
    return jsonify({
        'order_id':order_id, 'payment_type':'card', 'card':os.getenv('MONO_CARD'),
        'amount_uah':amount_uah, 'product_name':c['name'], 'duration_days':duration_days, 'quantity':quantity,
        'discount_percent':pct,
        'buyer_username':username,
        'payment_comment':username,
        'instructions':'В комментарии к переводу обязательно укажи свой Telegram @username. Если username нет — укажи Telegram ID.'
    })


@app.route('/api/orders/<int:order_id>/receipt', methods=['POST'])
@user_required
def upload_receipt(order_id):
    _d = deny_banned()
    if _d: return _d
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
               f"💵 {order['amount_uah'] or order['price_rub']} UAH × {order['quantity'] or 1} шт.\n"
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
    return jsonify(catalog(True))


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

        def opt_int(v):
            try:
                v = int(v)
                return v if v > 0 else None
            except (TypeError, ValueError):
                return None
        video = str(data.get('video_url') or '').strip()
        extra = {
            'devices':[d for d in (data.get('devices') or []) if d in ('ios','android','pc')],
            'status':data.get('status') if data.get('status') in ('working','updating','down') else 'working',
            'risk':data.get('risk') if data.get('risk') in ('low','mid','high') else None,
            'version':str(data.get('version') or '')[:30],
            'updated_at':str(data.get('updated_at') or '')[:10] or None,
            'badges':[b for b in (data.get('badges') or []) if b in ('new','hit')],
            'instruction':str(data.get('instruction') or '')[:5000],
            'old_price_rub':opt_int(data.get('old_price_rub')),
            'old_price_stars':opt_int(data.get('old_price_stars')),
            'video_url':video if video.startswith(('http://','https://')) else '',
            'trial':bool(data.get('trial')),
        }
        prices = {}
        raw_prices = data.get('prices') or {}
        for d in durations:
            r = raw_prices.get(str(d))
            if r:
                u = int(r.get('uah', 0)); st = int(r.get('stars', 0))
                if u < 0 or st < 1: raise ValueError()
                prices[str(d)] = {'uah':u, 'stars':st, 'old_uah':opt_int(r.get('old_uah'))}
        if prices:
            payload['price_rub'] = min(v['uah'] for v in prices.values())
            payload['price_stars'] = min(v['stars'] for v in prices.values())
            cheapest = min(prices.values(), key=lambda v: v['uah'])
            extra['old_price_rub'] = cheapest['old_uah']
            extra['old_price_stars'] = None
        extra['prices'] = prices
        new_shots = []
        for item in (data.get('new_screenshots') or [])[:5]:
            raw = item.get('image_data','')
            if ',' in raw: raw = raw.split(',',1)[1]
            content = base64.b64decode(raw, validate=True)
            smime = item.get('image_mime','image/jpeg')
            if smime not in ('image/jpeg','image/png','image/webp') or not content or len(content)>MAX_UPLOAD: raise ValueError()
            new_shots.append((content, smime))
        keep_ids = None
        if 'keep_screenshots' in data:
            keep_ids = [int(m.group(1)) for u in (data.get('keep_screenshots') or [])
                        for m in [re.search(r'/shot/(\d+)', str(u))] if m]
            if len(keep_ids) + len(new_shots) > 5: raise ValueError()
        new_id = run_async(db.save_cheat(payload, product_id))
        if not new_id: return jsonify({'error':'Не удалось сохранить товар'}),404
        run_async(db.set_extra(new_id, extra))
        if keep_ids is not None:
            run_async(db.prune_shots(new_id, keep_ids))
        for content, smime in new_shots:
            run_async(db.add_shot(new_id, content, smime))
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
    except (TypeError, ValueError):
        return jsonify({'error':'Выбери срок ключей'}),400
    if duration <= 0 or duration > 3650:
        return jsonify({'error':'Некорректный срок'}),400
    raw = data.get('keys','')
    if isinstance(raw, list): raw = '\n'.join(str(x) for x in raw)
    keys=[x.strip() for x in str(raw).splitlines() if x.strip()]
    if not keys: return jsonify({'error':'Список ключей пуст (по одному на строку)'}),400
    try:
        # админ явно добавляет ключи на этот срок — включаем срок у товара автоматически
        run_async(db.add_duration(product_id, duration))
        added=run_async(db.add_keys(product_id,keys,duration))
        if added > 0:
            try:
                subs = run_async(db.subscribers(cheat['game']))
                notify_async(subs, f"🔑 Новый завоз ключей для - «{cheat['game']}»")
            except Exception:
                logging.exception('subscribers notify failed')
        return jsonify({'ok':True,'added':added,'skipped':len(keys)-added})
    except Exception:
        logging.exception('add_product_keys failed')
        return jsonify({'error':'Не удалось сохранить ключи, смотри логи сервера'}),500


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


# ===================== СКЛАД: УДАЛЕНИЕ СВОБОДНЫХ КЛЮЧЕЙ =====================
@app.route('/api/admin/products/<int:product_id>/keys')
@admin_required
def admin_list_keys(product_id):
    duration = request.args.get('duration', type=int)
    if not duration: return jsonify([])
    rows = run_async(db.list_free_keys(product_id, duration))
    return jsonify([{'id':r['id'],'key':r['key_value']} for r in rows])


@app.route('/api/admin/keys/<int:key_id>', methods=['DELETE'])
@admin_required
def admin_delete_key(key_id):
    n = run_async(db.delete_free_key(key_id))
    if not n: return jsonify({'error':'Ключ не найден или уже выдан'}), 404
    return jsonify({'ok':True})


@app.route('/api/admin/products/<int:product_id>/keys/clear', methods=['POST'])
@admin_required
def admin_clear_keys(product_id):
    data = request.get_json(silent=True) or {}
    try: duration = int(data.get('duration_days'))
    except (TypeError, ValueError): return jsonify({'error':'Выбери срок'}), 400
    return jsonify({'ok':True,'deleted':run_async(db.clear_free_keys(product_id, duration))})


# ===================== КЛИЕНТЫ =====================
@app.route('/api/admin/clients')
@admin_required
def admin_clients():
    rows = run_async(db.list_clients(request.args.get('q', '')))
    return jsonify([json_safe(r) for r in rows])


@app.route('/api/admin/clients/<int:uid>')
@admin_required
def admin_client(uid):
    u = run_async(db.get_user(uid))
    if not u: return jsonify({'error':'Клиент не найден'}), 404
    orders = run_async(db.list_orders(user_id=uid))
    return jsonify({'user':json_safe(u), 'orders':[json_safe(o) for o in orders]})


@app.route('/api/admin/clients/<int:uid>/ban', methods=['POST'])
@admin_required
def admin_ban_client(uid):
    if uid == ADMIN_ID: return jsonify({'error':'Себя заблокировать нельзя'}), 400
    banned = bool((request.get_json(silent=True) or {}).get('banned'))
    run_async(db.set_banned(uid, banned))
    _BAN_CACHE[uid] = (banned, time.time())
    if banned: notify_async([uid], '🚫 Ваш аккаунт заблокирован. Если это ошибка — напишите в поддержку.', button=False)
    else: notify_async([uid], '✅ Ваш аккаунт разблокирован.', button=True)
    return jsonify({'ok':True,'banned':banned})


@app.route('/api/admin/clients/<int:uid>', methods=['DELETE'])
@admin_required
def admin_delete_client(uid):
    if uid == ADMIN_ID: return jsonify({'error':'Свой профиль удалить нельзя'}), 400
    run_async(db.delete_user(uid))
    _BAN_CACHE.pop(uid, None)
    return jsonify({'ok':True})


# ===================== ОЧИСТКА ИНВЕНТАРЯ =====================
@app.route('/api/inventory/clear', methods=['POST'])
@user_required
def clear_inventory():
    data = request.get_json(silent=True) or {}
    user = request.tg_user
    if data.get('order_id') is not None:
        try: oid = int(data.get('order_id'))
        except (TypeError, ValueError): return jsonify({'error':'Некорректный ключ'}), 400
        cleared = run_async(db.hide_inventory(user['id'], order_id=oid))
    elif data.get('scope') in ('expired', 'all'):
        cleared = run_async(db.hide_inventory(user['id'], scope=data['scope']))
    else:
        return jsonify({'error':'Что очистить?'}), 400
    return jsonify({'ok':True,'cleared':cleared})


# ===================== НОВОСТИ / РАССЫЛКА =====================
@app.route('/api/news')
def get_news():
    try: return jsonify([json_safe(n) for n in run_async(db.list_news())])
    except Exception:
        logging.exception('get_news failed')
        return jsonify([])


@app.route('/api/admin/news', methods=['POST'])
@admin_required
def admin_add_news():
    data = request.get_json(silent=True) or {}
    body = str(data.get('body') or '').strip()[:1000]
    if not body: return jsonify({'error':'Пустая новость'}), 400
    nid = run_async(db.add_news(str(data.get('title') or '').strip()[:120], body, bool(data.get('pinned'))))
    return jsonify({'ok':True,'id':nid})


@app.route('/api/admin/news/<int:news_id>', methods=['PUT'])
@admin_required
def admin_edit_news(news_id):
    data = request.get_json(silent=True) or {}
    body = str(data.get('body') or '').strip()[:1000]
    if not body: return jsonify({'error':'Пустая новость'}), 400
    done = run_async(db.update_news(news_id, str(data.get('title') or '').strip()[:120], body, bool(data.get('pinned'))))
    if not done: return jsonify({'error':'Новость не найдена'}), 404
    return jsonify({'ok':True})


@app.route('/api/admin/news/<int:news_id>', methods=['DELETE'])
@admin_required
def admin_delete_news(news_id):
    run_async(db.delete_news(news_id))
    return jsonify({'ok':True})


@app.route('/api/admin/notify', methods=['POST'])
@admin_required
def admin_notify():
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()[:500]
    if not text: return jsonify({'error':'Пустое сообщение'}), 400
    ids = run_async(db.broadcast_ids())
    notify_async(ids, text)
    return jsonify({'ok':True,'queued':len(ids)})


# ===================== ОТЗЫВЫ =====================
@app.route('/api/reviews')
def get_reviews():
    try: return jsonify([json_safe(r) for r in run_async(db.list_reviews())])
    except Exception:
        logging.exception('get_reviews failed')
        return jsonify([])


def parse_review(data):
    rating = int(data.get('rating', 5))
    text = str(data.get('text') or '').strip()[:500]
    if rating < 1 or rating > 5 or not text: raise ValueError()
    return rating, text


@app.route('/api/reviews', methods=['POST'])
@user_required
def post_review():
    _d = deny_banned()
    if _d: return _d
    """Отзыв может оставить только тот, кто купил этот чит. Имя берётся из Telegram, время — текущее."""
    data = request.get_json(silent=True) or {}
    user = request.tg_user
    try:
        product_id = int(data.get('product_id'))
        rating, text = parse_review(data)
    except (TypeError, ValueError):
        return jsonify({'error':'Поставь оценку и напиши текст отзыва'}), 400
    if not run_async(db.has_bought(user['id'], product_id)):
        return jsonify({'error':'Отзыв можно оставить только после покупки этого чита'}), 403
    if run_async(db.has_reviewed(user['id'], product_id)):
        return jsonify({'error':'Ты уже оставил отзыв на этот чит'}), 409
    rid = run_async(db.add_review(product_id, int(user['id']), clean_username(user), rating, text, True))
    return jsonify({'ok':True,'id':rid})


@app.route('/api/admin/reviews', methods=['POST'])
@admin_required
def admin_add_review():
    data = request.get_json(silent=True) or {}
    try:
        rating, text = parse_review(data)
        author = str(data.get('author') or '').strip()[:40]
        if not author: raise ValueError()
        pid = data.get('product_id')
        pid = int(pid) if pid not in (None, '') else None
        rid = run_async(db.add_review(pid, None, author, rating, text, False, data.get('created_at')))
        return jsonify({'ok':True,'id':rid})
    except Exception:
        logging.exception('admin_add_review failed')
        return jsonify({'error':'Заполни имя, оценку, текст и дату'}), 400


@app.route('/api/admin/reviews/<int:review_id>', methods=['PUT'])
@admin_required
def admin_edit_review(review_id):
    data = request.get_json(silent=True) or {}
    try:
        rating, text = parse_review(data)
        author = str(data.get('author') or '').strip()[:40]
        if not author: raise ValueError()
        pid = data.get('product_id')
        pid = int(pid) if pid not in (None, '') else None
        done = run_async(db.update_review(review_id, pid, author, rating, text, data.get('created_at') or datetime.utcnow()))
        if not done: return jsonify({'error':'Отзыв не найден'}), 404
        return jsonify({'ok':True})
    except Exception:
        logging.exception('admin_edit_review failed')
        return jsonify({'error':'Заполни имя, оценку, текст и дату'}), 400


@app.route('/api/admin/reviews/<int:review_id>', methods=['DELETE'])
@admin_required
def admin_delete_review(review_id):
    run_async(db.delete_review(review_id))
    return jsonify({'ok':True})


# ===================== ПОДПИСКИ / РЕФЕРАЛЫ / ПРОБНЫЙ КЛЮЧ =====================
@app.route('/api/subscriptions', methods=['POST'])
@user_required
def subscriptions():
    _d = deny_banned()
    if _d: return _d
    data = request.get_json(silent=True) or {}
    game = str(data.get('game') or '').strip()
    if not game: return jsonify({'error':'Не указана игра'}), 400
    user = request.tg_user
    run_async(db.upsert_user(user['id'], user.get('username'), user.get('first_name')))
    run_async(db.set_subscription(user['id'], game, bool(data.get('on'))))
    return jsonify({'ok':True})


@app.route('/api/referrals')
@user_required
def referrals():
    total, paid = run_async(db.referral_stats(request.tg_user['id']))
    return jsonify({'count':total,'paid':paid,'bonus':f"−{min(10, paid*2)}% НА ПОКУПКИ"})


@app.route('/api/trial', methods=['POST'])
@user_required
def trial():
    _d = deny_banned()
    if _d: return _d
    data = request.get_json(silent=True) or {}
    try: product_id = int(data.get('product_id'))
    except (TypeError, ValueError): return jsonify({'error':'Некорректный товар'}), 400
    c = run_async(db.get_cheat(product_id))
    if not c or not c['is_active'] or not decode_json(c['extra'], {}).get('trial'):
        return jsonify({'error':'Для этого чита нет пробного ключа'}), 404
    user = request.tg_user
    res = run_async(db.take_trial(user['id'], user.get('username'), product_id, decode_json(c['durations'], [30])))
    if not res['ok']:
        return jsonify({'error':'Пробный ключ уже получен' if res['reason']=='used' else 'Сейчас нет свободных ключей'}), 409
    return jsonify({'ok':True,'key':res['key']})


# ===================== ТИКЕТЫ =====================
@app.route('/api/tickets')
@user_required
def my_tickets():
    return jsonify([json_safe(t) for t in run_async(db.list_tickets(request.tg_user['id']))])


@app.route('/api/tickets', methods=['POST'])
@user_required
def new_ticket():
    data = request.get_json(silent=True) or {}
    subject = str(data.get('subject') or '').strip()[:80]
    text = str(data.get('text') or '').strip()[:1000]
    if not subject or not text: return jsonify({'error':'Заполни тему и сообщение'}), 400
    user = request.tg_user
    tid = run_async(db.create_ticket(user['id'], user.get('username'), subject, text))
    if ADMIN_ID: notify_async([ADMIN_ID], f"🎫 Новый тикет #{tid} от {clean_username(user)}\n{subject}\n\n{text}\n\nОтветь в приложении: Админ → ТИКЕТЫ", button=True)
    return jsonify({'ok':True,'id':tid})


@app.route('/api/tickets/<int:ticket_id>/reply', methods=['POST'])
@user_required
def reply_ticket(ticket_id):
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()[:1000]
    t = run_async(db.get_ticket(ticket_id))
    if not t or int(t['user_id']) != int(request.tg_user['id']): return jsonify({'error':'Тикет не найден'}), 404
    if t['status'] == 'closed': return jsonify({'error':'Тикет закрыт'}), 409
    if not text: return jsonify({'error':'Пустое сообщение'}), 400
    run_async(db.add_ticket_message(ticket_id, 'user', text))
    if ADMIN_ID: notify_async([ADMIN_ID], f"💬 Ответ в тикете #{ticket_id} от {clean_username(request.tg_user)}:\n\n{text}", button=True)
    return jsonify(json_safe(run_async(db.get_ticket(ticket_id))))


@app.route('/api/admin/tickets')
@admin_required
def admin_tickets():
    return jsonify([json_safe(t) for t in run_async(db.list_tickets())])


@app.route('/api/admin/tickets/<int:ticket_id>/reply', methods=['POST'])
@admin_required
def admin_reply_ticket(ticket_id):
    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '').strip()[:1000]
    t = run_async(db.get_ticket(ticket_id))
    if not t: return jsonify({'error':'Тикет не найден'}), 404
    if not text: return jsonify({'error':'Пустое сообщение'}), 400
    run_async(db.add_ticket_message(ticket_id, 'admin', text))
    notify_async([int(t['user_id'])], f"💬 Ответ поддержки по тикету #{ticket_id}:\n\n{text}")
    return jsonify(json_safe(run_async(db.get_ticket(ticket_id))))


@app.route('/api/admin/tickets/<int:ticket_id>/close', methods=['POST'])
@admin_required
def admin_close_ticket(ticket_id):
    run_async(db.close_ticket(ticket_id))
    return jsonify({'ok':True})


# ===================== СТАТИСТИКА / ПРОМОКОДЫ =====================
@app.route('/api/admin/stats')
@admin_required
def admin_stats():
    return jsonify(run_async(db.stats()))


@app.route('/api/admin/promos')
@admin_required
def admin_promos():
    return jsonify([json_safe(p) for p in run_async(db.list_promos())])


@app.route('/api/admin/promos', methods=['POST'])
@admin_required
def admin_add_promo():
    data = request.get_json(silent=True) or {}
    try:
        code = re.sub(r'[^A-Za-z0-9_-]', '', str(data.get('code') or '')).upper()[:24]
        percent = int(data.get('percent')); max_uses = int(data.get('max_uses') or 0)
        if not code or percent < 1 or percent > 90 or max_uses < 0: raise ValueError()
        pid = run_async(db.add_promo(code, percent, max_uses))
        return jsonify({'ok':True,'id':pid})
    except Exception:
        return jsonify({'error':'Код (латиница/цифры), скидка 1–90%'}), 400


@app.route('/api/admin/promos/<int:promo_id>', methods=['DELETE'])
@admin_required
def admin_delete_promo(promo_id):
    run_async(db.delete_promo(promo_id))
    return jsonify({'ok':True})


def run_bot():
    import bot
    asyncio.run(bot.main())


if __name__ == '__main__':
    bot_thread=threading.Thread(target=run_bot,daemon=True)
    bot_thread.start()
    port=int(os.getenv('PORT',5000))
    app.run(host='0.0.0.0',port=port)

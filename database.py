import asyncio
import asyncpg
import json
import os

_pools = {}

async def _get_pool():
    loop = asyncio.get_running_loop()
    pool = _pools.get(id(loop))
    if pool is None:
        pool = await asyncpg.create_pool(os.getenv('DATABASE_URL'), min_size=1, max_size=8)
        _pools[id(loop)] = pool
    return pool

class _Conn:
    """Обёртка: conn.close() возвращает соединение в пул (остальной код не меняется)."""
    def __init__(self, conn, pool):
        self._c = conn
        self._p = pool
    def __getattr__(self, name):
        return getattr(self._c, name)
    async def close(self):
        if self._c is not None:
            c, self._c = self._c, None
            await self._p.release(c)

async def get_db():
    pool = await _get_pool()
    return _Conn(await pool.acquire(), pool)

NEW_TABLES = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
        referrer_id BIGINT, created_at TIMESTAMP DEFAULT NOW(), last_seen TIMESTAMP DEFAULT NOW(),
        blocked BOOLEAN NOT NULL DEFAULT FALSE)""",
    """CREATE TABLE IF NOT EXISTS news (
        id SERIAL PRIMARY KEY, title TEXT, body TEXT NOT NULL,
        pinned BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS reviews (
        id SERIAL PRIMARY KEY, product_id INTEGER, user_id BIGINT, author TEXT NOT NULL,
        rating INTEGER NOT NULL DEFAULT 5, text TEXT NOT NULL,
        verified BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        user_id BIGINT NOT NULL, game TEXT NOT NULL, PRIMARY KEY (user_id, game))""",
    """CREATE TABLE IF NOT EXISTS tickets (
        id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, username TEXT, subject TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', created_at TIMESTAMP DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS ticket_messages (
        id SERIAL PRIMARY KEY, ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
        sender TEXT NOT NULL, text TEXT NOT NULL, created_at TIMESTAMP DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS promos (
        id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, percent INTEGER NOT NULL,
        max_uses INTEGER NOT NULL DEFAULT 0, uses INTEGER NOT NULL DEFAULT 0)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_user_product ON reviews(user_id, product_id) WHERE user_id IS NOT NULL AND verified=TRUE",
    "ALTER TABLE cheats ADD COLUMN IF NOT EXISTS extra TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS promo_code TEXT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS is_trial BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE TABLE IF NOT EXISTS trials (user_id BIGINT NOT NULL, cheat_id INTEGER NOT NULL, PRIMARY KEY (user_id, cheat_id))",
    "CREATE TABLE IF NOT EXISTS product_images (id SERIAL PRIMARY KEY, cheat_id INTEGER NOT NULL, data BYTEA NOT NULL, mime TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_product_images_cheat ON product_images(cheat_id)",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS quantity INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS amount_uah INTEGER",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS amount_stars INTEGER",
    "INSERT INTO users (user_id, username) SELECT DISTINCT ON (user_id) user_id, username FROM orders ORDER BY user_id, created_at DESC ON CONFLICT (user_id) DO NOTHING",
]

async def init_db():
    conn = await get_db()
    try:
        await conn.execute('SELECT pg_advisory_lock(727001)')
        await conn.execute('''CREATE TABLE IF NOT EXISTS cheats (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, game TEXT NOT NULL,
            device TEXT NOT NULL, description TEXT, price_rub INTEGER NOT NULL,
            price_stars INTEGER NOT NULL)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS keys (
            id SERIAL PRIMARY KEY, cheat_id INTEGER REFERENCES cheats(id),
            key_value TEXT NOT NULL, is_used BOOLEAN DEFAULT FALSE)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, username TEXT,
            cheat_id INTEGER REFERENCES cheats(id), payment_type TEXT NOT NULL,
            status TEXT DEFAULT 'pending', receipt_file_id TEXT, key_given TEXT,
            created_at TIMESTAMP DEFAULT NOW())''')
        migrations = [
            "ALTER TABLE cheats ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'Без категории'",
            "ALTER TABLE cheats ADD COLUMN IF NOT EXISTS features TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE cheats ADD COLUMN IF NOT EXISTS durations TEXT NOT NULL DEFAULT '[30]'",
            "ALTER TABLE cheats ADD COLUMN IF NOT EXISTS image_data BYTEA",
            "ALTER TABLE cheats ADD COLUMN IF NOT EXISTS image_mime TEXT",
            "ALTER TABLE cheats ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE keys ADD COLUMN IF NOT EXISTS duration_days INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS duration_days INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS telegram_payment_charge_id TEXT",
        ]
        for sql in migrations:
            await conn.execute(sql)
        for sql in NEW_TABLES:
            await conn.execute(sql)
        await conn.execute("UPDATE orders SET confirmed_at=created_at WHERE status='confirmed' AND confirmed_at IS NULL")
        await conn.execute("UPDATE orders SET expires_at=COALESCE(confirmed_at,created_at)+(duration_days*INTERVAL '1 day') WHERE status='confirmed' AND expires_at IS NULL")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_keys_available ON keys(cheat_id, duration_days, is_used)")
    finally:
        try: await conn.execute('SELECT pg_advisory_unlock(727001)')
        except Exception: pass
        await conn.close()

async def get_cheats(include_inactive=False):
    conn = await get_db()
    try:
        q = "SELECT * FROM cheats" if include_inactive else "SELECT * FROM cheats WHERE is_active=TRUE"
        return await conn.fetch(q + " ORDER BY id DESC")
    finally: await conn.close()

async def get_cheat(cheat_id):
    conn = await get_db()
    try: return await conn.fetchrow("SELECT * FROM cheats WHERE id=$1", cheat_id)
    finally: await conn.close()

async def save_cheat(data, cheat_id=None):
    fields = [data.get('name','').strip(), data.get('game','').strip(), data.get('device','').strip(),
              data.get('description',''), int(data.get('price_rub',0)), int(data.get('price_stars',0)),
              data.get('category','Без категории'), json.dumps(data.get('features',[]), ensure_ascii=False),
              json.dumps(data.get('durations',[30]))]
    conn = await get_db()
    try:
        if cheat_id:
            if data.get('image_data') is not None:
                return await conn.fetchval('''UPDATE cheats SET name=$2,game=$3,device=$4,description=$5,
                    price_rub=$6,price_stars=$7,category=$8,features=$9,durations=$10,image_data=$11,image_mime=$12,
                    is_active=TRUE WHERE id=$1 RETURNING id''', int(cheat_id), *fields, data['image_data'], data.get('image_mime'))
            return await conn.fetchval('''UPDATE cheats SET name=$2,game=$3,device=$4,description=$5,
                price_rub=$6,price_stars=$7,category=$8,features=$9,durations=$10,is_active=TRUE
                WHERE id=$1 RETURNING id''', int(cheat_id), *fields)
        return await conn.fetchval('''INSERT INTO cheats (name,game,device,description,price_rub,price_stars,category,features,durations,image_data,image_mime)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id''', *fields, data.get('image_data'), data.get('image_mime'))
    finally: await conn.close()

async def add_cheat(name, game, device, description, price_rub, price_stars, category='Без категории', features=None, durations=None):
    return await save_cheat({'name':name,'game':game,'device':device,'description':description,'price_rub':price_rub,
        'price_stars':price_stars,'category':category,'features':features or [],'durations':durations or [30]})

async def delete_cheat(cheat_id):
    # Archive, rather than hard-delete, to preserve historic orders and foreign keys.
    conn = await get_db()
    try: await conn.execute('UPDATE cheats SET is_active=FALSE WHERE id=$1', int(cheat_id))
    finally: await conn.close()

async def add_key(cheat_id, key_value, duration_days=30):
    return await add_keys(cheat_id, [key_value], duration_days)

async def add_keys(cheat_id, key_values, duration_days=30):
    unique_values = {}
    for value in key_values:
        clean = str(value).strip()
        if clean:
            unique_values.setdefault(clean.casefold(), clean)
    values = list(unique_values.values())
    if not values:
        return 0
    conn = await get_db()
    try:
        async with conn.transaction():
            # Serialize inserts for identical credentials without deleting existing rows.
            for value in sorted(values, key=str.casefold):
                await conn.fetchval('SELECT pg_advisory_xact_lock(hashtext(LOWER($1)))', value)
            existing = await conn.fetch(
                'SELECT LOWER(key_value) AS normalized FROM keys WHERE LOWER(key_value)=ANY($1::TEXT[])',
                [value.casefold() for value in values])
            present = {row['normalized'] for row in existing}
            fresh = [(int(cheat_id), value, int(duration_days))
                     for value in values if value.casefold() not in present]
            if fresh:
                await conn.executemany(
                    'INSERT INTO keys (cheat_id,key_value,duration_days) VALUES ($1,$2,$3)',
                    fresh)
        return len(fresh)
    finally:
        await conn.close()

async def get_free_key(cheat_id, duration_days=None):
    conn = await get_db()
    try:
        if duration_days is None:
            return await conn.fetchrow('''SELECT k.* FROM keys k WHERE k.cheat_id=$1 AND k.is_used=FALSE
                AND NOT EXISTS (SELECT 1 FROM keys prior WHERE LOWER(prior.key_value)=LOWER(k.key_value) AND prior.id<k.id)
                AND NOT EXISTS (SELECT 1 FROM keys used WHERE LOWER(used.key_value)=LOWER(k.key_value) AND used.is_used=TRUE)
                ORDER BY k.id LIMIT 1''', int(cheat_id))
        return await conn.fetchrow('''SELECT k.* FROM keys k WHERE k.cheat_id=$1 AND k.duration_days=$2 AND k.is_used=FALSE
            AND NOT EXISTS (SELECT 1 FROM keys prior WHERE LOWER(prior.key_value)=LOWER(k.key_value) AND prior.id<k.id)
            AND NOT EXISTS (SELECT 1 FROM keys used WHERE LOWER(used.key_value)=LOWER(k.key_value) AND used.is_used=TRUE)
            ORDER BY k.id LIMIT 1''', int(cheat_id), int(duration_days))
    finally: await conn.close()

async def keys_count(cheat_id):
    conn = await get_db()
    try:
        rows = await conn.fetch('''SELECT k.duration_days, COUNT(*) AS amount FROM keys k
            WHERE k.cheat_id=$1 AND k.is_used=FALSE
              AND NOT EXISTS (SELECT 1 FROM keys prior WHERE LOWER(prior.key_value)=LOWER(k.key_value) AND prior.id<k.id)
              AND NOT EXISTS (SELECT 1 FROM keys used WHERE LOWER(used.key_value)=LOWER(k.key_value) AND used.is_used=TRUE)
            GROUP BY k.duration_days''', int(cheat_id))
        return {int(r['duration_days']):int(r['amount']) for r in rows}
    finally: await conn.close()

async def create_order(user_id, username, cheat_id, payment_type, duration_days=30, quantity=1,
                       amount_uah=None, amount_stars=None, promo_code=None):
    conn = await get_db()
    try:
        return await conn.fetchval('''INSERT INTO orders (user_id,username,cheat_id,payment_type,duration_days,quantity,amount_uah,amount_stars,promo_code)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id''', int(user_id), username, int(cheat_id), payment_type,
            int(duration_days), int(quantity), amount_uah, amount_stars, promo_code)
    finally: await conn.close()

async def get_order(order_id):
    conn = await get_db()
    try: return await conn.fetchrow('SELECT * FROM orders WHERE id=$1', int(order_id))
    finally: await conn.close()

async def get_order_details(order_id):
    conn = await get_db()
    try:
        return await conn.fetchrow('''SELECT o.*, c.name, c.game, c.device, c.price_rub, c.price_stars
            FROM orders o LEFT JOIN cheats c ON c.id=o.cheat_id WHERE o.id=$1''', int(order_id))
    finally: await conn.close()

async def update_order_receipt(order_id, file_id):
    conn = await get_db()
    try:
        return await conn.execute("UPDATE orders SET receipt_file_id=$1 WHERE id=$2 AND status='pending'", file_id, int(order_id))
    finally: await conn.close()

class _NoKey(Exception):
    pass

_PICK_KEY_SQL = '''SELECT * FROM keys k
    WHERE k.cheat_id=$1 AND k.duration_days=$2 AND k.is_used=FALSE
      AND NOT EXISTS (SELECT 1 FROM keys prior WHERE LOWER(prior.key_value)=LOWER(k.key_value) AND prior.id<k.id)
      AND NOT EXISTS (SELECT 1 FROM keys used WHERE LOWER(used.key_value)=LOWER(k.key_value) AND used.is_used=TRUE)
    ORDER BY k.id LIMIT 1 FOR UPDATE OF k SKIP LOCKED'''

async def _take_key(conn, cheat_id, duration_days):
    while True:
        candidate = await conn.fetchrow(_PICK_KEY_SQL, cheat_id, duration_days)
        if not candidate:
            return None
        await conn.fetchval('SELECT pg_advisory_xact_lock(hashtext(LOWER($1)))', candidate['key_value'])
        already_used = await conn.fetchval(
            'SELECT EXISTS(SELECT 1 FROM keys WHERE LOWER(key_value)=LOWER($1) AND is_used=TRUE)', candidate['key_value'])
        if already_used:
            continue
        await conn.execute('UPDATE keys SET is_used=TRUE WHERE id=$1', candidate['id'])
        return candidate

async def approve_order(order_id, charge_id=None):
    """Atomically assign matching key(s); validity begins at confirmation."""
    conn = await get_db()
    try:
        try:
            async with conn.transaction():
                order = await conn.fetchrow('SELECT * FROM orders WHERE id=$1 FOR UPDATE', int(order_id))
                if not order: return {'ok':False,'reason':'not_found'}
                if order['status'] == 'confirmed': return {'ok':True,'already':True,'order':dict(order),'key':order['key_given']}
                if order['status'] != 'pending': return {'ok':False,'reason':'not_pending'}
                taken = []
                for _ in range(max(1, int(order['quantity'] or 1))):
                    key = await _take_key(conn, order['cheat_id'], order['duration_days'])
                    if not key:
                        raise _NoKey()
                    taken.append(key['key_value'])
                joined = '\n'.join(taken)
                row = await conn.fetchrow('''UPDATE orders SET status='confirmed',key_given=$2,confirmed_at=NOW(),
                    expires_at=NOW() + ($3 * INTERVAL '1 day'),telegram_payment_charge_id=COALESCE($4,telegram_payment_charge_id)
                    WHERE id=$1 RETURNING *''', int(order_id), joined, int(order['duration_days']), charge_id)
                if order['promo_code']:
                    await conn.execute('UPDATE promos SET uses=uses+1 WHERE code=$1', order['promo_code'])
                return {'ok':True,'already':False,'order':dict(row),'key':joined}
        except _NoKey:
            return {'ok':False,'reason':'no_key'}
    finally:
        await conn.close()

async def confirm_order(order_id, key_value):
    """Legacy confirmation path; reserve a credential only if it is still unused."""
    conn = await get_db()
    try:
        async with conn.transaction():
            order = await conn.fetchrow('SELECT * FROM orders WHERE id=$1 FOR UPDATE', int(order_id))
            if not order or order['status'] != 'pending':
                return False
            key = await conn.fetchrow('''SELECT k.id FROM keys k WHERE k.cheat_id=$1 AND k.duration_days=$2
                AND k.key_value=$3 AND k.is_used=FALSE
                AND NOT EXISTS (SELECT 1 FROM keys prior WHERE LOWER(prior.key_value)=LOWER(k.key_value) AND prior.id<k.id)
                ORDER BY k.id LIMIT 1 FOR UPDATE OF k SKIP LOCKED''',
                order['cheat_id'], order['duration_days'], key_value)
            if not key:
                return False
            await conn.fetchval('SELECT pg_advisory_xact_lock(hashtext(LOWER($1)))', key_value)
            used = await conn.fetchval(
                'SELECT EXISTS(SELECT 1 FROM keys WHERE LOWER(key_value)=LOWER($1) AND is_used=TRUE)',
                key_value)
            if used:
                return False
            await conn.execute("UPDATE orders SET status='confirmed',key_given=$1,confirmed_at=NOW(),expires_at=NOW()+(duration_days*INTERVAL '1 day') WHERE id=$2 AND status='pending'", key_value, int(order_id))
            await conn.execute('UPDATE keys SET is_used=TRUE WHERE id=$1', key['id'])
            return True
    finally: await conn.close()

async def reject_order(order_id):
    conn = await get_db()
    try: return await conn.execute("UPDATE orders SET status='rejected' WHERE id=$1 AND status='pending'", int(order_id))
    finally: await conn.close()

async def list_orders(user_id=None, pending_only=False):
    conn = await get_db()
    try:
        parts, args = [], []
        if user_id is not None: args.append(int(user_id)); parts.append(f'o.user_id=${len(args)}')
        if pending_only: parts.append("o.status='pending'")
        where = (' WHERE ' + ' AND '.join(parts)) if parts else ''
        return await conn.fetch('''SELECT o.*,c.name,c.game,c.device,c.price_rub,c.price_stars
            FROM orders o LEFT JOIN cheats c ON c.id=o.cheat_id''' + where + ' ORDER BY o.created_at DESC LIMIT 100', *args)
    finally: await conn.close()

async def get_inventory(user_id):
    conn = await get_db()
    try:
        return await conn.fetch('''SELECT o.id AS order_id,o.key_given,o.duration_days,o.confirmed_at,o.expires_at,o.quantity,o.is_trial,
            c.id AS cheat_id,c.id AS product_id,c.name,c.game,c.device,c.category,c.extra
            FROM orders o LEFT JOIN cheats c ON c.id=o.cheat_id
            WHERE o.user_id=$1 AND o.status='confirmed' AND o.hidden=FALSE ORDER BY o.confirmed_at DESC''', int(user_id))
    finally: await conn.close()


# ===================== ДОПОЛНЕНИЯ =====================
from datetime import datetime, timezone

def _naive_utc(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

async def fetch(sql, *args):
    conn = await get_db()
    try: return await conn.fetch(sql, *args)
    finally: await conn.close()

async def fetchrow(sql, *args):
    conn = await get_db()
    try: return await conn.fetchrow(sql, *args)
    finally: await conn.close()

async def fetchval(sql, *args):
    conn = await get_db()
    try: return await conn.fetchval(sql, *args)
    finally: await conn.close()

async def execute(sql, *args):
    conn = await get_db()
    try: return await conn.execute(sql, *args)
    finally: await conn.close()

# ---- склад: одним запросом для всех товаров ----
async def keys_count_all():
    rows = await fetch('''SELECT k.cheat_id, k.duration_days, COUNT(*) AS amount FROM keys k
        WHERE k.is_used=FALSE
          AND NOT EXISTS (SELECT 1 FROM keys prior WHERE LOWER(prior.key_value)=LOWER(k.key_value) AND prior.id<k.id)
          AND NOT EXISTS (SELECT 1 FROM keys used WHERE LOWER(used.key_value)=LOWER(k.key_value) AND used.is_used=TRUE)
        GROUP BY k.cheat_id, k.duration_days''')
    out = {}
    for r in rows:
        out.setdefault(int(r['cheat_id']), {})[int(r['duration_days'])] = int(r['amount'])
    return out

async def add_duration(cheat_id, duration):
    row = await fetchrow('SELECT durations FROM cheats WHERE id=$1', int(cheat_id))
    try: cur = [int(x) for x in json.loads(row['durations'] or '[30]')]
    except Exception: cur = [30]
    if int(duration) not in cur:
        cur = sorted(set(cur + [int(duration)]))
        await execute('UPDATE cheats SET durations=$2 WHERE id=$1', int(cheat_id), json.dumps(cur))
    return cur

# ---- доп. поля товара и скриншоты ----
async def set_extra(cheat_id, extra):
    await execute('UPDATE cheats SET extra=$2 WHERE id=$1', int(cheat_id), json.dumps(extra, ensure_ascii=False))

async def add_shot(cheat_id, data, mime):
    return await fetchval('INSERT INTO product_images (cheat_id,data,mime) VALUES ($1,$2,$3) RETURNING id', int(cheat_id), data, mime)

async def shots_map():
    rows = await fetch('SELECT id, cheat_id FROM product_images ORDER BY id')
    out = {}
    for r in rows:
        out.setdefault(int(r['cheat_id']), []).append(int(r['id']))
    return out

async def prune_shots(cheat_id, keep_ids):
    await execute('DELETE FROM product_images WHERE cheat_id=$1 AND NOT (id = ANY($2::INT[]))', int(cheat_id), [int(x) for x in keep_ids])

async def get_shot(shot_id):
    return await fetchrow('SELECT data, mime FROM product_images WHERE id=$1', int(shot_id))

# ---- пользователи ----
async def upsert_user(user_id, username=None, first_name=None, referrer_id=None):
    ref = int(referrer_id) if referrer_id and int(referrer_id) != int(user_id) else None
    await execute('''INSERT INTO users (user_id,username,first_name,referrer_id) VALUES ($1,$2,$3,$4)
        ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name,
        last_seen=NOW(), blocked=FALSE''', int(user_id), username, first_name, ref)

async def mark_blocked(user_id):
    await execute('UPDATE users SET blocked=TRUE WHERE user_id=$1', int(user_id))

async def broadcast_ids():
    return [int(r['user_id']) for r in await fetch('SELECT user_id FROM users WHERE blocked=FALSE AND banned=FALSE')]

async def referral_stats(user_id):
    row = await fetchrow('''SELECT COUNT(*) AS total,
        COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id=u.user_id AND o.status='confirmed' AND NOT o.is_trial)) AS paid
        FROM users u WHERE u.referrer_id=$1''', int(user_id))
    return int(row['total']), int(row['paid'])

async def purchases_count(user_id):
    return int(await fetchval("SELECT COUNT(*) FROM orders WHERE user_id=$1 AND status='confirmed' AND NOT is_trial", int(user_id)) or 0)

# ---- новости ----
async def list_news():
    return await fetch('SELECT * FROM news ORDER BY pinned DESC, created_at DESC LIMIT 50')

async def add_news(title, body, pinned=False):
    return await fetchval('INSERT INTO news (title,body,pinned) VALUES ($1,$2,$3) RETURNING id', title, body, bool(pinned))

async def delete_news(news_id):
    await execute('DELETE FROM news WHERE id=$1', int(news_id))

# ---- отзывы ----
async def list_reviews():
    return await fetch('SELECT id,product_id,author,rating,text,verified,created_at FROM reviews ORDER BY created_at DESC LIMIT 500')

async def get_review(review_id):
    return await fetchrow('SELECT * FROM reviews WHERE id=$1', int(review_id))

async def add_review(product_id, user_id, author, rating, text, verified, created_at=None):
    when = _naive_utc(created_at) if created_at else datetime.utcnow()
    return await fetchval('''INSERT INTO reviews (product_id,user_id,author,rating,text,verified,created_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id''', product_id, user_id, author, int(rating), text, bool(verified), when)

async def update_review(review_id, product_id, author, rating, text, created_at):
    return await fetchval('''UPDATE reviews SET product_id=$2,author=$3,rating=$4,text=$5,created_at=$6
        WHERE id=$1 RETURNING id''', int(review_id), product_id, author, int(rating), text, _naive_utc(created_at))

async def delete_review(review_id):
    await execute('DELETE FROM reviews WHERE id=$1', int(review_id))

async def has_bought(user_id, cheat_id):
    return bool(await fetchval("SELECT EXISTS(SELECT 1 FROM orders WHERE user_id=$1 AND cheat_id=$2 AND status='confirmed' AND NOT is_trial)", int(user_id), int(cheat_id)))

async def has_reviewed(user_id, cheat_id):
    return bool(await fetchval("SELECT EXISTS(SELECT 1 FROM reviews WHERE user_id=$1 AND product_id=$2 AND verified=TRUE)", int(user_id), int(cheat_id)))

# ---- подписки на игры ----
async def set_subscription(user_id, game, on):
    if on:
        await execute('INSERT INTO subscriptions (user_id,game) VALUES ($1,$2) ON CONFLICT DO NOTHING', int(user_id), game.casefold())
    else:
        await execute('DELETE FROM subscriptions WHERE user_id=$1 AND game=$2', int(user_id), game.casefold())

async def subscribers(game):
    return [int(r['user_id']) for r in await fetch('SELECT s.user_id FROM subscriptions s JOIN users u ON u.user_id=s.user_id WHERE s.game=$1 AND u.blocked=FALSE AND u.banned=FALSE', game.casefold())]

# ---- тикеты ----
async def _with_messages(tickets):
    if not tickets: return []
    ids = [int(t['id']) for t in tickets]
    msgs = await fetch('SELECT * FROM ticket_messages WHERE ticket_id = ANY($1::INT[]) ORDER BY id', ids)
    by = {}
    for m in msgs:
        by.setdefault(int(m['ticket_id']), []).append({'from':m['sender'],'text':m['text'],'created_at':m['created_at']})
    out = []
    for t in tickets:
        d = dict(t); d['messages'] = by.get(int(t['id']), []); out.append(d)
    return out

async def create_ticket(user_id, username, subject, text):
    tid = await fetchval('INSERT INTO tickets (user_id,username,subject) VALUES ($1,$2,$3) RETURNING id', int(user_id), username, subject)
    await execute("INSERT INTO ticket_messages (ticket_id,sender,text) VALUES ($1,'user',$2)", tid, text)
    return tid

async def list_tickets(user_id=None):
    if user_id is None:
        rows = await fetch('SELECT * FROM tickets ORDER BY (status=\'open\') DESC, id DESC LIMIT 100')
    else:
        rows = await fetch('SELECT * FROM tickets WHERE user_id=$1 ORDER BY id DESC LIMIT 50', int(user_id))
    return await _with_messages(rows)

async def get_ticket(ticket_id):
    rows = await fetch('SELECT * FROM tickets WHERE id=$1', int(ticket_id))
    res = await _with_messages(rows)
    return res[0] if res else None

async def add_ticket_message(ticket_id, sender, text):
    await execute('INSERT INTO ticket_messages (ticket_id,sender,text) VALUES ($1,$2,$3)', int(ticket_id), sender, text)
    if sender == 'user':
        await execute("UPDATE tickets SET status='open' WHERE id=$1", int(ticket_id))

async def close_ticket(ticket_id):
    await execute("UPDATE tickets SET status='closed' WHERE id=$1", int(ticket_id))

# ---- промокоды ----
async def list_promos():
    return await fetch('SELECT * FROM promos ORDER BY id DESC')

async def add_promo(code, percent, max_uses):
    return await fetchval('INSERT INTO promos (code,percent,max_uses) VALUES ($1,$2,$3) RETURNING id', code, int(percent), int(max_uses))

async def delete_promo(promo_id):
    await execute('DELETE FROM promos WHERE id=$1', int(promo_id))

async def find_promo(code):
    if not code: return None
    row = await fetchrow('SELECT * FROM promos WHERE UPPER(code)=UPPER($1)', code.strip())
    if not row: return None
    if int(row['max_uses']) and int(row['uses']) >= int(row['max_uses']): return None
    return row

# ---- пробный ключ ----
async def take_trial(user_id, username, cheat_id, durations):
    conn = await get_db()
    try:
        try:
            async with conn.transaction():
                ins = await conn.fetchval('INSERT INTO trials (user_id,cheat_id) VALUES ($1,$2) ON CONFLICT DO NOTHING RETURNING 1', int(user_id), int(cheat_id))
                if not ins:
                    return {'ok': False, 'reason': 'used'}
                for d in sorted(int(x) for x in durations):
                    key = await _take_key(conn, int(cheat_id), d)
                    if key:
                        await conn.execute('''INSERT INTO orders (user_id,username,cheat_id,payment_type,status,key_given,duration_days,
                            confirmed_at,expires_at,is_trial,quantity) VALUES ($1,$2,$3,'trial','confirmed',$4,$5,NOW(),NOW()+INTERVAL '1 hour',TRUE,1)''',
                            int(user_id), username, int(cheat_id), key['key_value'], d)
                        return {'ok': True, 'key': key['key_value']}
                raise _NoKey()
        except _NoKey:
            return {'ok': False, 'reason': 'no_key'}
    finally:
        await conn.close()

# ---- статистика ----
async def stats():
    async def period(cond):
        row = await fetchrow(f"""SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN o.payment_type='card'
            THEN COALESCE(o.amount_uah, c.price_rub*o.quantity) ELSE 0 END),0) AS rev
            FROM orders o LEFT JOIN cheats c ON c.id=o.cheat_id
            WHERE o.status='confirmed' AND NOT o.is_trial {cond}""")
        return {'orders': int(row['n']), 'revenue': int(row['rev'])}
    top = await fetch("""SELECT c.name, COUNT(*) AS sales FROM orders o JOIN cheats c ON c.id=o.cheat_id
        WHERE o.status='confirmed' AND NOT o.is_trial GROUP BY c.name ORDER BY sales DESC LIMIT 5""")
    return {
        'today': await period("AND o.confirmed_at >= date_trunc('day', NOW())"),
        'week': await period("AND o.confirmed_at >= NOW() - INTERVAL '7 days'"),
        'month': await period("AND o.confirmed_at >= NOW() - INTERVAL '30 days'"),
        'total': await period(""),
        'top': [{'name': r['name'], 'sales': int(r['sales'])} for r in top],
    }


# ---- очистка инвентаря (скрытие из профиля; история заказов, ранг и отзывы сохраняются) ----
async def hide_inventory(user_id, order_id=None, scope=None):
    if order_id is not None:
        res = await execute("UPDATE orders SET hidden=TRUE WHERE id=$1 AND user_id=$2 AND status='confirmed'", int(order_id), int(user_id))
    elif scope == 'expired':
        res = await execute("UPDATE orders SET hidden=TRUE WHERE user_id=$1 AND status='confirmed' AND expires_at<=NOW()", int(user_id))
    elif scope == 'all':
        res = await execute("UPDATE orders SET hidden=TRUE WHERE user_id=$1 AND status='confirmed'", int(user_id))
    else:
        return 0
    try: return int(str(res).split()[-1])
    except Exception: return 0


# ---- склад: просмотр и удаление СВОБОДНЫХ ключей (выданные не трогаем) ----
async def list_free_keys(cheat_id, duration_days):
    return await fetch('''SELECT id, key_value FROM keys WHERE cheat_id=$1 AND duration_days=$2 AND is_used=FALSE
        ORDER BY id LIMIT 300''', int(cheat_id), int(duration_days))

async def delete_free_key(key_id):
    res = await execute('DELETE FROM keys WHERE id=$1 AND is_used=FALSE', int(key_id))
    try: return int(str(res).split()[-1])
    except Exception: return 0

async def clear_free_keys(cheat_id, duration_days):
    res = await execute('DELETE FROM keys WHERE cheat_id=$1 AND duration_days=$2 AND is_used=FALSE', int(cheat_id), int(duration_days))
    try: return int(str(res).split()[-1])
    except Exception: return 0

# ---- новости: редактирование ----
async def update_news(news_id, title, body, pinned):
    return await fetchval('UPDATE news SET title=$2, body=$3, pinned=$4 WHERE id=$1 RETURNING id', int(news_id), title, body, bool(pinned))

# ---- клиенты ----
async def list_clients(q=''):
    q = (q or '').strip().lstrip('@')
    return await fetch('''SELECT u.user_id, u.username, u.first_name, u.created_at, u.last_seen, u.banned,
        (SELECT COUNT(*) FROM orders o WHERE o.user_id=u.user_id AND o.status='confirmed' AND NOT o.is_trial) AS purchases,
        (SELECT COALESCE(SUM(COALESCE(o.amount_uah, c.price_rub)),0) FROM orders o LEFT JOIN cheats c ON c.id=o.cheat_id
            WHERE o.user_id=u.user_id AND o.status='confirmed' AND NOT o.is_trial AND o.payment_type='card') AS spent
        FROM users u
        WHERE ($1='' OR u.username ILIKE $2 OR u.first_name ILIKE $2 OR u.user_id::text=$1)
        ORDER BY u.last_seen DESC NULLS LAST LIMIT 100''', q, f'%{q}%')

async def get_user(user_id):
    return await fetchrow('SELECT * FROM users WHERE user_id=$1', int(user_id))

async def set_banned(user_id, banned):
    await execute('UPDATE users SET banned=$2 WHERE user_id=$1', int(user_id), bool(banned))

async def is_banned(user_id):
    return bool(await fetchval('SELECT banned FROM users WHERE user_id=$1', int(user_id)))

async def delete_user(user_id):
    conn = await get_db()
    try:
        async with conn.transaction():
            uid = int(user_id)
            await conn.execute('DELETE FROM ticket_messages WHERE ticket_id IN (SELECT id FROM tickets WHERE user_id=$1)', uid)
            await conn.execute('DELETE FROM tickets WHERE user_id=$1', uid)
            await conn.execute('DELETE FROM reviews WHERE user_id=$1', uid)
            await conn.execute('DELETE FROM subscriptions WHERE user_id=$1', uid)
            await conn.execute('DELETE FROM trials WHERE user_id=$1', uid)
            await conn.execute('DELETE FROM orders WHERE user_id=$1', uid)
            await conn.execute('UPDATE users SET referrer_id=NULL WHERE referrer_id=$1', uid)
            await conn.execute('DELETE FROM users WHERE user_id=$1', uid)
    finally:
        await conn.close()

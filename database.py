import asyncpg
import json
import os

async def get_db():
    return await asyncpg.connect(os.getenv('DATABASE_URL'))

async def init_db():
    conn = await get_db()
    try:
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
        await conn.execute("UPDATE orders SET confirmed_at=created_at WHERE status='confirmed' AND confirmed_at IS NULL")
        await conn.execute("UPDATE orders SET expires_at=COALESCE(confirmed_at,created_at)+(duration_days*INTERVAL '1 day') WHERE status='confirmed' AND expires_at IS NULL")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_keys_available ON keys(cheat_id, duration_days, is_used)")
    finally:
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
    conn = await get_db()
    try: await conn.execute('INSERT INTO keys (cheat_id,key_value,duration_days) VALUES ($1,$2,$3)', int(cheat_id), key_value.strip(), int(duration_days))
    finally: await conn.close()

async def get_free_key(cheat_id, duration_days=None):
    conn = await get_db()
    try:
        if duration_days is None:
            return await conn.fetchrow('SELECT * FROM keys WHERE cheat_id=$1 AND is_used=FALSE ORDER BY id LIMIT 1', int(cheat_id))
        return await conn.fetchrow('SELECT * FROM keys WHERE cheat_id=$1 AND duration_days=$2 AND is_used=FALSE ORDER BY id LIMIT 1', int(cheat_id), int(duration_days))
    finally: await conn.close()

async def keys_count(cheat_id):
    conn = await get_db()
    try:
        rows = await conn.fetch('SELECT duration_days, COUNT(*) AS amount FROM keys WHERE cheat_id=$1 AND is_used=FALSE GROUP BY duration_days', int(cheat_id))
        return {int(r['duration_days']):int(r['amount']) for r in rows}
    finally: await conn.close()

async def create_order(user_id, username, cheat_id, payment_type, duration_days=30):
    conn = await get_db()
    try:
        return await conn.fetchval('''INSERT INTO orders (user_id,username,cheat_id,payment_type,duration_days)
            VALUES ($1,$2,$3,$4,$5) RETURNING id''', int(user_id), username, int(cheat_id), payment_type, int(duration_days))
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

async def approve_order(order_id, charge_id=None):
    """Atomically assign one matching key; its validity begins at confirmation."""
    conn = await get_db()
    try:
        async with conn.transaction():
            order = await conn.fetchrow('SELECT * FROM orders WHERE id=$1 FOR UPDATE', int(order_id))
            if not order: return {'ok':False,'reason':'not_found'}
            if order['status'] == 'confirmed': return {'ok':True,'already':True,'order':dict(order),'key':order['key_given']}
            if order['status'] != 'pending': return {'ok':False,'reason':'not_pending'}
            key = await conn.fetchrow('''SELECT * FROM keys WHERE cheat_id=$1 AND duration_days=$2 AND is_used=FALSE
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED''', order['cheat_id'], order['duration_days'])
            if not key: return {'ok':False,'reason':'no_key'}
            row = await conn.fetchrow('''UPDATE orders SET status='confirmed',key_given=$2,confirmed_at=NOW(),
                expires_at=NOW() + ($3 * INTERVAL '1 day'),telegram_payment_charge_id=COALESCE($4,telegram_payment_charge_id)
                WHERE id=$1 RETURNING *''', int(order_id), key['key_value'], int(order['duration_days']), charge_id)
            await conn.execute('UPDATE keys SET is_used=TRUE WHERE id=$1', key['id'])
            return {'ok':True,'already':False,'order':dict(row),'key':key['key_value']}
    finally: await conn.close()

async def confirm_order(order_id, key_value):
    conn = await get_db()
    try:
        await conn.execute("UPDATE orders SET status='confirmed',key_given=$1,confirmed_at=NOW(),expires_at=NOW()+(duration_days*INTERVAL '1 day') WHERE id=$2 AND status='pending'", key_value, int(order_id))
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
        return await conn.fetch('''SELECT o.id AS order_id,o.key_given,o.duration_days,o.confirmed_at,o.expires_at,
            c.id AS cheat_id,c.name,c.game,c.device,c.category
            FROM orders o LEFT JOIN cheats c ON c.id=o.cheat_id
            WHERE o.user_id=$1 AND o.status='confirmed' ORDER BY o.confirmed_at DESC''', int(user_id))
    finally: await conn.close()

import asyncpg
import os

async def get_db():
    return await asyncpg.connect(os.getenv("DATABASE_URL"))

async def init_db():
    conn = await get_db()
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS cheats (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            game TEXT NOT NULL,
            device TEXT NOT NULL,
            description TEXT,
            price_rub INTEGER NOT NULL,
            price_stars INTEGER NOT NULL
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS keys (
            id SERIAL PRIMARY KEY,
            cheat_id INTEGER REFERENCES cheats(id),
            key_value TEXT NOT NULL,
            is_used BOOLEAN DEFAULT FALSE
        )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            username TEXT,
            cheat_id INTEGER REFERENCES cheats(id),
            payment_type TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            receipt_file_id TEXT,
            key_given TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    ''')
    await conn.close()

async def add_cheat(name, game, device, description, price_rub, price_stars):
    conn = await get_db()
    result = await conn.fetchrow(
        "INSERT INTO cheats (name, game, device, description, price_rub, price_stars) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
        name, game, device, description, price_rub, price_stars
    )
    await conn.close()
    return result['id']

async def add_key(cheat_id, key_value):
    conn = await get_db()
    await conn.execute(
        "INSERT INTO keys (cheat_id, key_value) VALUES ($1, $2)",
        cheat_id, key_value
    )
    await conn.close()

async def get_cheats():
    conn = await get_db()
    rows = await conn.fetch("SELECT * FROM cheats")
    await conn.close()
    return rows

async def get_cheat(cheat_id):
    conn = await get_db()
    row = await conn.fetchrow("SELECT * FROM cheats WHERE id=$1", cheat_id)
    await conn.close()
    return row

async def get_free_key(cheat_id):
    conn = await get_db()
    row = await conn.fetchrow(
        "SELECT * FROM keys WHERE cheat_id=$1 AND is_used=FALSE LIMIT 1",
        cheat_id
    )
    await conn.close()
    return row

async def mark_key_used(key_id):
    conn = await get_db()
    await conn.execute("UPDATE keys SET is_used=TRUE WHERE id=$1", key_id)
    await conn.close()

async def create_order(user_id, username, cheat_id, payment_type):
    conn = await get_db()
    result = await conn.fetchrow(
        "INSERT INTO orders (user_id, username, cheat_id, payment_type) VALUES ($1,$2,$3,$4) RETURNING id",
        user_id, username, cheat_id, payment_type
    )
    await conn.close()
    return result['id']

async def update_order_receipt(order_id, file_id):
    conn = await get_db()
    await conn.execute(
        "UPDATE orders SET receipt_file_id=$1 WHERE id=$2",
        file_id, order_id
    )
    await conn.close()

async def get_order(order_id):
    conn = await get_db()
    row = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
    await conn.close()
    return row

async def confirm_order(order_id, key_value):
    conn = await get_db()
    await conn.execute(
        "UPDATE orders SET status='confirmed', key_given=$1 WHERE id=$2",
        key_value, order_id
    )
    await conn.close()

async def reject_order(order_id):
    conn = await get_db()
    await conn.execute(
        "UPDATE orders SET status='rejected' WHERE id=$1",
        order_id
    )
    await conn.close()

async def delete_cheat(cheat_id):
    conn = await get_db()
    await conn.execute("DELETE FROM keys WHERE cheat_id=$1", cheat_id)
    await conn.execute("DELETE FROM cheats WHERE id=$1", cheat_id)
    await conn.close()

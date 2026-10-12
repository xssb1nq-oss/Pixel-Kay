import os
import asyncio
import threading
import logging
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv
import database as db

load_dotenv()

app = Flask(__name__)


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@app.route("/")
def index():
    return send_from_directory(app.root_path, "index.html")


@app.route("/api/cheats")
def get_cheats():
    try:
        cheats = run_async(db.get_cheats())
        result = []
        for c in cheats:
            result.append({
                "id": c["id"],
                "name": c["name"],
                "game": c["game"],
                "device": c["device"],
                "description": c["description"],
                "price_rub": c["price_rub"],
                "price_stars": c["price_stars"]
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cheat/<int:cheat_id>")
def get_cheat(cheat_id):
    try:
        cheat = run_async(db.get_cheat(cheat_id))
        if not cheat:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "id": cheat["id"],
            "name": cheat["name"],
            "game": cheat["game"],
            "device": cheat["device"],
            "description": cheat["description"],
            "price_rub": cheat["price_rub"],
            "price_stars": cheat["price_stars"]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/keys_count/<int:cheat_id>")
def keys_count(cheat_id):
    try:
        key = run_async(db.get_free_key(cheat_id))
        return jsonify({"available": key is not None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run_bot():
    import bot
    asyncio.run(bot.main())


if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

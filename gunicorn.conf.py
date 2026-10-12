"""Run one Telegram polling bot alongside the single Gunicorn web worker."""
import asyncio
import logging
import threading

workers = 1
threads = 4
timeout = 120
preload_app = False

def _run_bot():
    try:
        import bot
        asyncio.run(bot.main())
    except Exception:
        logging.exception('Telegram bot thread stopped')

def post_worker_init(worker):
    threading.Thread(target=_run_bot, name='telegram-polling', daemon=True).start()

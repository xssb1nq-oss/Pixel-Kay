"""Точка входа. Роль выбирается переменной окружения ROLE:
  ROLE=web  (по умолчанию) — только сайт и API (gunicorn)
  ROLE=bot                 — только Telegram-бот
  ROLE=all                 — и сайт, и бот в одном сервисе
"""
import os
import subprocess
import sys

role = os.getenv('ROLE', 'web').strip().lower()
port = os.getenv('PORT', '8080')
GUNICORN = ['gunicorn', 'app:app', '--config', 'gunicorn.conf.py', '--bind', f'0.0.0.0:{port}']

if role == 'bot':
    import asyncio
    import bot
    asyncio.run(bot.main())
elif role == 'all':
    import asyncio
    import bot
    web = subprocess.Popen(GUNICORN)
    try:
        asyncio.run(bot.main())
    finally:
        web.terminate()
else:
    os.execvp('gunicorn', GUNICORN)

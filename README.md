# Pager AI Bot (Zambia test)

Telegram-бот + воркер для [Pager.co.ua](https://www.pager.co.ua): каждый пользователь TG подключает **свой** аккаунт Pager, бот автоматически отправляет **скрипты ZM** в Messenger, в Telegram приходят только **эскалации** (депозит, жалобы, ID не распознан).

## Быстрый старт

```powershell
cd C:\Users\user\Projects\pager-ai-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# Ключ шифрования
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

copy .env.example .env
# BOT_TOKEN, ENCRYPTION_KEY, опционально OPENAI_API_KEY

python bot.py
```

## Подключение Pager (в боте)

1. **🔐 Pager аккаунт** → **Email + пароль** (Playwright)  
   или **🍪 Импорт cookies** (надёжнее): DevTools → Network → Cookie header  
2. **📡 Каналы** → включить Kelvin Phiri (ZM)  
3. `/escalation` — куда слать «нужен человек» (по умолчанию — ваш TG id)

## Команды

| Команда | Действие |
|---------|----------|
| `/pause` | Пауза авто-ответов |
| `/resume` | Включить снова |
| `/status` | Сессия, каналы |

## Воронка ZM (скрипты в `data/scripts/zm/`)

1. interested → intro  
2. ok → how it works + ZMW  
3. yes → registration + link → папка «В процесі реєстрації»  
4. скрин-пример → «Чекаю ID»  
5. фото/ID → депозит-скрипт → «Реєстрація»  
6. скрин депозита → TG + TG-канал  

## Railway

Деплой через **Dockerfile** (внутри уже Chromium для входа по email/паролю).

Variables: `BOT_TOKEN`, `ENCRYPTION_KEY`, `OPENAI_API_KEY` (optional)

Опционально для ссылок «Открыть Pager»:
- `PAGER_ORG_SLUG=tehsup` — slug организации в URL
- `PAGER_LOCALE=uk` — язык (по умолчанию `uk`)

Ссылка в TG: `https://www.pager.co.ua/uk/tehsup/chats?channelId=...`  
Pager **не открывает конкретный чат** по URL — только канал; имя клиента в уведомлении.

После push в GitHub: Railway → **Redeploy** (сборка ~2–3 мин, образ больше обычного).

Cookies-импорт остаётся запасным вариантом.

## Структура

- `bot.py` — Telegram + worker  
- `services/pager_api.py` — API Pager  
- `services/worker_loop.py` — опрос чатов  
- `database.py` — аккаунты пользователей (SQLite)

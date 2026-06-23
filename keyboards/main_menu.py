from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔐 Pager аккаунт"), KeyboardButton(text="📡 Каналы")],
            [KeyboardButton(text="📂 Выбор папок")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="ℹ️ Статус")],
        ],
        resize_keyboard=True,
    )


def connect_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Email + пароль", callback_data="pager:login")],
            [InlineKeyboardButton(text="🍪 Импорт cookies", callback_data="pager:cookies")],
            [InlineKeyboardButton(text="❌ Отключить", callback_data="pager:disconnect")],
        ]
    )


from database import normalize_channel_geo, next_channel_geo

GEO_BUTTON_LABELS = {
    "zm": "🇿🇲 ZM",
    "eg": "🇪🇬 EG",
    "dj": "🇩🇯 DJ",
}


def channel_geo_label(ch: dict, *, account_geo: str = "zm") -> str:
    raw = str(ch.get("geo") or "").strip().lower()
    geo = normalize_channel_geo(raw, default=account_geo) if raw else normalize_channel_geo(account_geo)
    return GEO_BUTTON_LABELS.get(geo, geo.upper())


def channels_kb(channels: list[dict], *, account_geo: str = "zm") -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        en = ch.get("enabled")
        mark = "✅" if en else "⬜"
        cid = ch.get("channel_id")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {ch.get('name') or cid}",
                    callback_data=f"ch:toggle:{cid}",
                ),
                InlineKeyboardButton(
                    text=channel_geo_label(ch, account_geo=account_geo),
                    callback_data=f"ch:geo:{cid}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="⬜ Выкл все", callback_data="ch:all_off"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="ch:refresh"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def folders_kb(folder_rows: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for i, folder in enumerate(folder_rows):
        mark = "✅" if folder.get("enabled") else "⬜"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {folder.get('name')}",
                    callback_data=f"fld:t:{i}",
                )
            ]
        )
    all_on = bool(folder_rows) and all(folder.get("enabled") for folder in folder_rows)
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Все папки вкл." if all_on else "📂 Включить все",
                callback_data="fld:on",
            ),
            InlineKeyboardButton(text="⬜ Снять все", callback_data="fld:off"),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="🔄 Обновить папки", callback_data="fld:sync")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

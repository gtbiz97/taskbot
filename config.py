"""Загрузка конфигурации из .env."""
import os
from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = _int_list(os.getenv("ADMIN_IDS", ""))

DB_PATH = os.getenv("DB_PATH", "taskbot.db")

# ---- Google Sheets (Apps Script webhook) ----
SHEETS_ENABLED = os.getenv("SHEETS_ENABLED", "0") == "1"
GOOGLE_SHEETS_WEBHOOK = os.getenv("GOOGLE_SHEETS_WEBHOOK", "")
SHEETS_TOKEN = os.getenv("SHEETS_TOKEN", "")
# Ссылка на таблицу-реестр (для команды /table). Не секрет: доступ к таблице — по правам Google.
SHEET_URL = os.getenv("SHEET_URL", "https://docs.google.com/spreadsheets/d/1_TCrhPNXrtEwugspD-FRn86-90vsGg9hp-CwC17uXsQ/edit")

WEEKLY_REPORT_DAY = os.getenv("WEEKLY_REPORT_DAY", "fri")
WEEKLY_REPORT_TIME = os.getenv("WEEKLY_REPORT_TIME", "18:00")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

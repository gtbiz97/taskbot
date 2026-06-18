"""Выгрузка данных из SQLite в Google Sheets.

Безопасно работает, даже если Sheets выключен или библиотека недоступна —
в этом случае функции просто ничего не делают (бот продолжает работать).
"""
import logging

import config
import db

log = logging.getLogger("sheets")

_client = None

HEADERS = [
    "ID", "Сотрудник", "@username", "Задача", "Описание",
    "Статус", "Дедлайн", "Создана", "Обновлена", "Отчёты",
]


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not config.SHEETS_ENABLED:
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(config.GOOGLE_CREDENTIALS, scopes=scopes)
        _client = gspread.authorize(creds)
        return _client
    except Exception as e:  # noqa: BLE001
        log.warning("Google Sheets недоступен: %s", e)
        return None


def _worksheet(title: str, rows: int = 1000, cols: int = 20):
    client = _get_client()
    if client is None:
        return None
    sh = client.open_by_key(config.SPREADSHEET_ID)
    try:
        return sh.worksheet(title)
    except Exception:  # noqa: BLE001
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def _report_text(task_id: int) -> str:
    reps = db.reports_for_task(task_id)
    return " | ".join(r["text"] for r in reps)


def sync_all_tasks():
    """Полностью перезаписывает лист «Задачи» текущим состоянием БД."""
    ws = _worksheet("Задачи")
    if ws is None:
        return False
    rows = [HEADERS]
    for t in db.all_tasks():
        rows.append([
            t["id"],
            t["employee_name"],
            f"@{t['employee_username']}" if t["employee_username"] else "",
            t["title"],
            t["description"] or "",
            db.STATUS_LABELS.get(t["status"], t["status"]),
            t["deadline"] or "",
            t["created_at"],
            t["updated_at"],
            _report_text(t["id"]),
        ])
    ws.clear()
    ws.update(rows, value_input_option="RAW")
    log.info("Sheets: выгружено задач: %d", len(rows) - 1)
    return True


def append_weekly_report(period_label: str, summary_rows: list[list]):
    """Добавляет блок недельного отчёта на отдельный лист «Отчёты»."""
    ws = _worksheet("Отчёты")
    if ws is None:
        return False
    block = [[f"Отчёт за {period_label}"]]
    block += [["Сотрудник", "Всего", "Сделано", "Не сделано", "В работе"]]
    block += summary_rows
    block += [[""]]
    ws.append_rows(block, value_input_option="RAW")
    return True

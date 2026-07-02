"""Выгрузка данных в Google Sheets через Apps Script веб-приёмник (webhook).

Бот шлёт JSON по защищённой ссылке (GOOGLE_SHEETS_WEBHOOK). Скрипт на стороне
таблицы пишет данные в листы «Задачи» и «Отчёты». Секрет — только URL и токен.
Если выключено или недоступно — функции тихо ничего не делают (бот работает).
"""
import json
import logging
import urllib.request

import config
import db

log = logging.getLogger("sheets")

last_error = ""

HEADERS = [
    "ID", "Сотрудник", "@username", "Название", "Описание",
    "Статус", "Дедлайн", "Создана", "Обновлена", "Отчёты",
]


def _post(payload: dict) -> bool:
    global last_error
    last_error = ""
    if not config.SHEETS_ENABLED or not config.GOOGLE_SHEETS_WEBHOOK:
        last_error = "SHEETS_ENABLED=0 или нет GOOGLE_SHEETS_WEBHOOK"
        return False
    payload["token"] = config.SHEETS_TOKEN
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        config.GOOGLE_SHEETS_WEBHOOK, data=data,
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; TaskBot/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "ignore")
            if "ok" not in body.lower():
                last_error = "ответ webhook: " + body[:200]
                log.warning("Sheets webhook ответ: %s", body)
                return False
            return True
    except Exception as e:  # noqa: BLE001
        last_error = f"{type(e).__name__}: {e}"
        log.warning("Sheets webhook ошибка: %s", e)
        return False


def _report_text(task_id: int) -> str:
    return " | ".join(r["text"] for r in db.reports_for_task(task_id))


def sync_all_tasks() -> bool:
    """Перезаписывает лист «Задачи» текущим состоянием БД."""
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
    return _post({"action": "sync_tasks", "rows": rows})


def append_weekly_report(period_label: str, summary_rows: list) -> bool:
    """Добавляет блок недельного отчёта на лист «Отчёты»."""
    block = [[f"Отчёт за {period_label}"],
             ["Сотрудник", "Всего", "Сделано", "Не сделано", "В работе", "Отменено"]]
    block += summary_rows
    block += [[""]]
    return _post({"action": "weekly", "rows": block})

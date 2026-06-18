"""Быстрый тест логики БД и отчётов без Telegram.

Запуск:  python test_smoke.py
Использует временную БД, Google Sheets выключен.
"""
import os

os.environ["SHEETS_ENABLED"] = "0"
os.environ["DB_PATH"] = "test_taskbot.db"
os.environ["ADMIN_IDS"] = "999"

import config  # noqa: E402
import db       # noqa: E402
import reports  # noqa: E402

if os.path.exists(config.DB_PATH):
    os.remove(config.DB_PATH)

db.init_db()
db.upsert_employee(1, "ivan", "Иван Иванов")
db.upsert_employee(2, "petr", "Пётр Петров")

t1 = db.create_task(1, 999, "Сделать отчёт", deadline="до пт")
t2 = db.create_task(1, 999, "Позвонить клиенту")
t3 = db.create_task(2, 999, "Подготовить смету")

db.set_status(t1, db.STATUS_DONE, 1)
db.add_report(t1, 1, "Отчёт готов, отправил на почту")
db.set_status(t2, db.STATUS_FAILED, 1)
db.set_status(t3, db.STATUS_PROGRESS, 2)

text, rows, label = reports.build_weekly()
print(text)
print("\nСтроки для Sheets:", rows)

assert len(db.list_employees()) == 2
assert rows == [["Иван Иванов", 2, 1, 1, 0], ["Пётр Петров", 1, 0, 0, 1]]
print("\n✅ Все проверки прошли.")

os.remove(config.DB_PATH)

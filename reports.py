"""Формирование еженедельного отчёта: что сделано / что нет по сотрудникам."""
from collections import defaultdict
from datetime import date

import db
import sheets


def build_weekly(ref: date | None = None):
    """Возвращает (текст_для_telegram, строки_для_sheets, period_label)."""
    start, end = db.week_bounds(ref)
    period_label = f"{start[:10]} — {end[:10]}"
    tasks = db.tasks_in_period(start, end)

    by_emp = defaultdict(list)
    for t in tasks:
        by_emp[t["employee_name"]].append(t)

    lines = [f"📊 <b>Еженедельный отчёт</b> ({period_label})", ""]
    sheet_rows = []

    if not tasks:
        lines.append("За неделю задач не ставилось.")
        return "\n".join(lines), sheet_rows, period_label

    for emp_name, emp_tasks in by_emp.items():
        total = len(emp_tasks)
        done = sum(1 for t in emp_tasks if t["status"] == db.STATUS_DONE)
        failed = sum(1 for t in emp_tasks if t["status"] == db.STATUS_FAILED)
        in_work = sum(1 for t in emp_tasks if t["status"] in db.OPEN_STATUSES)

        lines.append(f"👤 <b>{emp_name}</b> — всего {total}: ✅ {done} | ❌ {failed} | 🟡 {in_work}")
        for t in emp_tasks:
            mark = db.STATUS_LABELS.get(t["status"], t["status"])
            reps = db.reports_for_task(t["id"])
            rep = f" — «{reps[-1]['text']}»" if reps else ""
            lines.append(f"   • {t['title']} [{mark}]{rep}")
        lines.append("")

        sheet_rows.append([emp_name, total, done, failed, in_work])

    text = "\n".join(lines)
    return text, sheet_rows, period_label


def export_weekly_to_sheets(ref: date | None = None):
    text, sheet_rows, period_label = build_weekly(ref)
    sheets.sync_all_tasks()
    if sheet_rows:
        sheets.append_weekly_report(period_label, sheet_rows)
    return text

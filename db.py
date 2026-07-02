"""Слой работы с БД (SQLite). Хранит сотрудников, задачи, историю статусов и отчёты."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from typing import Optional

import config

# Статусы задачи
STATUS_NEW = "new"            # поставлена, сотрудник ещё не отреагировал
STATUS_ACCEPTED = "accepted"  # взял в работу
STATUS_PROGRESS = "progress"  # в работе
STATUS_REVISION = "revision"  # отправлена на доработку
STATUS_DONE = "done"          # сделано
STATUS_FAILED = "failed"      # не успел / не сделано
STATUS_CANCELLED = "cancelled"  # отменена руководителем

STATUS_LABELS = {
    STATUS_NEW: "🆕 Новая",
    STATUS_ACCEPTED: "📥 Взял",
    STATUS_PROGRESS: "🟡 В работе",
    STATUS_REVISION: "🔁 На доработке",
    STATUS_DONE: "✅ Сделано",
    STATUS_FAILED: "❌ Не успел",
    STATUS_CANCELLED: "🚫 Отменена",
}

OPEN_STATUSES = (STATUS_NEW, STATUS_ACCEPTED, STATUS_PROGRESS, STATUS_REVISION)


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS employees (
                tg_id      INTEGER PRIMARY KEY,
                username   TEXT,
                full_name  TEXT,
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id  INTEGER NOT NULL,
                author_id    INTEGER NOT NULL,
                title        TEXT NOT NULL,
                description  TEXT,
                status       TEXT NOT NULL DEFAULT 'new',
                deadline     TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                FOREIGN KEY (employee_id) REFERENCES employees(tg_id)
            );

            CREATE TABLE IF NOT EXISTS status_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                status     TEXT NOT NULL,
                changed_by INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS reports (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                employee_id INTEGER NOT NULL,
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS task_updates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                field      TEXT NOT NULL,
                old_value  TEXT,
                new_value  TEXT,
                changed_by INTEGER NOT NULL,
                changed_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );
            """
        )


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def task_identifier(task_id: int) -> str:
    return f"#{task_id}"


# ---------- Сотрудники ----------
def upsert_employee(tg_id: int, username: str, full_name: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO employees (tg_id, username, full_name, active, created_at)
               VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username,
                                                full_name=excluded.full_name,
                                                active=1""",
            (tg_id, username, full_name, now()),
        )


def deactivate_employee(tg_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE employees SET active=0 WHERE tg_id=?", (tg_id,))


def list_employees(active_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM employees"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY full_name"
    with get_conn() as conn:
        return conn.execute(q).fetchall()


def get_employee(tg_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM employees WHERE tg_id=?", (tg_id,)).fetchone()


# ---------- Задачи ----------
def create_task(employee_id: int, author_id: int, title: str,
                description: str = "", deadline: str = "") -> int:
    ts = now()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO tasks (employee_id, author_id, title, description,
                                  status, deadline, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (employee_id, author_id, title, description, STATUS_NEW, deadline, ts, ts),
        )
        task_id = cur.lastrowid
        conn.execute(
            "INSERT INTO status_history (task_id, status, changed_by, changed_at) VALUES (?,?,?,?)",
            (task_id, STATUS_NEW, author_id, ts),
        )
    return task_id


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def set_status(task_id: int, status: str, changed_by: int):
    ts = now()
    with get_conn() as conn:
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, ts, task_id))
        conn.execute(
            "INSERT INTO status_history (task_id, status, changed_by, changed_at) VALUES (?,?,?,?)",
            (task_id, status, changed_by, ts),
        )


def cancel_task(task_id: int, changed_by: int) -> bool:
    ts = now()
    with get_conn() as conn:
        current = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not current:
            return False
        old_status = current["status"]
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (STATUS_CANCELLED, ts, task_id))
        conn.execute(
            "INSERT INTO status_history (task_id, status, changed_by, changed_at) VALUES (?,?,?,?)",
            (task_id, STATUS_CANCELLED, changed_by, ts),
        )
        conn.execute(
            """INSERT INTO task_updates (task_id, field, old_value, new_value, changed_by, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, "status", old_status, STATUS_CANCELLED, changed_by, ts),
        )
    return True


def send_task_to_revision(task_id: int, comment: str, changed_by: int) -> bool:
    ts = now()
    with get_conn() as conn:
        current = conn.execute(
            "SELECT status, description FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if not current:
            return False
        old_status = current["status"]
        old_description = current["description"] or ""
        entry = f"[{ts}] Доработка: {comment}"
        description = f"{old_description}\n\n{entry}" if old_description else entry
        conn.execute(
            "UPDATE tasks SET status=?, description=?, updated_at=? WHERE id=?",
            (STATUS_REVISION, description, ts, task_id),
        )
        conn.execute(
            "INSERT INTO status_history (task_id, status, changed_by, changed_at) VALUES (?,?,?,?)",
            (task_id, STATUS_REVISION, changed_by, ts),
        )
        conn.execute(
            """INSERT INTO task_updates (task_id, field, old_value, new_value, changed_by, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, "status", old_status, STATUS_REVISION, changed_by, ts),
        )
        conn.execute(
            """INSERT INTO task_updates (task_id, field, old_value, new_value, changed_by, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, "description", old_description, description, changed_by, ts),
        )
    return True


def update_task_title(task_id: int, title: str, changed_by: int) -> bool:
    ts = now()
    with get_conn() as conn:
        current = conn.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not current:
            return False
        conn.execute("UPDATE tasks SET title=?, updated_at=? WHERE id=?", (title, ts, task_id))
        conn.execute(
            """INSERT INTO task_updates (task_id, field, old_value, new_value, changed_by, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, "title", current["title"], title, changed_by, ts),
        )
    return True


def update_task_deadline(task_id: int, deadline: str, changed_by: int) -> bool:
    ts = now()
    with get_conn() as conn:
        current = conn.execute("SELECT deadline FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not current:
            return False
        conn.execute("UPDATE tasks SET deadline=?, updated_at=? WHERE id=?", (deadline, ts, task_id))
        conn.execute(
            """INSERT INTO task_updates (task_id, field, old_value, new_value, changed_by, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, "deadline", current["deadline"] or "", deadline, changed_by, ts),
        )
    return True


def append_task_description(task_id: int, addition: str, changed_by: int) -> bool:
    ts = now()
    with get_conn() as conn:
        current = conn.execute("SELECT description FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not current:
            return False
        old_description = current["description"] or ""
        entry = f"[{ts}] {addition}"
        description = f"{old_description}\n\n{entry}" if old_description else entry
        conn.execute(
            "UPDATE tasks SET description=?, updated_at=? WHERE id=?",
            (description, ts, task_id),
        )
        conn.execute(
            """INSERT INTO task_updates (task_id, field, old_value, new_value, changed_by, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, "description", old_description, description, changed_by, ts),
        )
    return True


def add_report(task_id: int, employee_id: int, text: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO reports (task_id, employee_id, text, created_at) VALUES (?,?,?,?)",
            (task_id, employee_id, text, now()),
        )


def tasks_for_employee(employee_id: int, only_open: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM tasks WHERE employee_id=?"
    params = [employee_id]
    if only_open:
        q += " AND status IN (%s)" % ",".join("?" * len(OPEN_STATUSES))
        params += list(OPEN_STATUSES)
    q += " ORDER BY created_at DESC"
    with get_conn() as conn:
        return conn.execute(q, params).fetchall()


def tasks_in_period(start: str, end: str) -> list[sqlite3.Row]:
    """Задачи, созданные или обновлённые в периоде [start, end)."""
    with get_conn() as conn:
        return conn.execute(
            """SELECT t.*, e.full_name AS employee_name, e.username AS employee_username
               FROM tasks t JOIN employees e ON e.tg_id = t.employee_id
               WHERE t.created_at >= ? AND t.created_at < ?
               ORDER BY e.full_name, t.created_at""",
            (start, end),
        ).fetchall()


def all_tasks() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT t.*, e.full_name AS employee_name, e.username AS employee_username
               FROM tasks t JOIN employees e ON e.tg_id = t.employee_id
               ORDER BY t.id""",
        ).fetchall()


def reports_for_task(task_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM reports WHERE task_id=? ORDER BY created_at", (task_id,)
        ).fetchall()


def week_bounds(ref: Optional[date] = None) -> tuple[str, str]:
    """Границы текущей недели (Пн 00:00 .. след. Пн 00:00) в ISO."""
    ref = ref or date.today()
    monday = ref - timedelta(days=ref.weekday())
    start = datetime.combine(monday, datetime.min.time())
    end = start + timedelta(days=7)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")

"""Telegram-бот: постановка задач, статусы, отчёты, недельная сводка.

Запуск:  python bot.py
"""
import asyncio
import logging
import subprocess
from html import escape

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import db
import keyboards as kb
import reports
import sheets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------- FSM состояния ----------
class NewTask(StatesGroup):
    pick = State()       # выбор сотрудников
    title = State()      # текст задачи
    deadline = State()   # дедлайн


class ReportFSM(StatesGroup):
    text = State()


class EditTask(StatesGroup):
    title = State()
    addition = State()
    deadline = State()


# ================= СОТРУДНИК =================
@dp.message(CommandStart())
async def cmd_start(m: Message):
    db.upsert_employee(m.from_user.id, m.from_user.username or "", m.from_user.full_name)
    if config.is_admin(m.from_user.id):
        await m.answer(
            "👋 Вы вошли как <b>руководитель</b>.\n\n"
            "/new — поставить задачу\n"
            "/tasks — открытые задачи с ID\n"
            "/task 12 — показать задачу по ID\n"
            "/edit 12 новый текст — отредактировать задачу\n"
            "/append 12 уточнение — дополнить задачу\n"
            "/deadline 12 новый дедлайн — изменить дедлайн\n"
            "/status — недельная сводка\n"
            "/report — собрать недельный отчёт сейчас\n"
            "/employees — список сотрудников\n"
            "/sync — выгрузить в Google Sheets\n"
            "/table — ссылка на таблицу-реестр\n\n"
            f"📊 <b>Реестр задач:</b> {config.SHEET_URL}"
        )
    else:
        await m.answer(
            "👋 Вы зарегистрированы в системе задач.\n"
            "Сюда будут приходить задачи с кнопками статуса. "
            "Команда /my покажет ваши открытые задачи."
        )


@dp.message(Command("my"))
async def cmd_my(m: Message):
    tasks = db.tasks_for_employee(m.from_user.id, only_open=True)
    if not tasks:
        await m.answer("У вас нет открытых задач 🎉")
        return
    for t in tasks:
        await m.answer(_task_card(t), reply_markup=kb.task_status_kb(t["id"]))


# ================= РУКОВОДИТЕЛЬ =================
@dp.message(Command("employees"))
async def cmd_employees(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    emps = db.list_employees()
    if not emps:
        await m.answer("Сотрудников пока нет. Попросите их нажать /start у бота.")
        return
    lines = ["<b>Сотрудники:</b>"]
    for e in emps:
        uname = f" (@{escape(e['username'])})" if e["username"] else ""
        lines.append(f"• {escape(e['full_name'])}{uname}")
    await m.answer("\n".join(lines))


@dp.message(Command("tasks"))
async def cmd_tasks(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    tasks = [t for t in db.all_tasks() if t["status"] in db.OPEN_STATUSES]
    if not tasks:
        await m.answer("Открытых задач нет.")
        return
    lines = ["<b>Открытые задачи:</b>"]
    for t in tasks:
        deadline = f", дедлайн: {escape(t['deadline'])}" if t["deadline"] else ""
        lines.append(
            f"• {db.task_identifier(t['id'])} {escape(t['title'])} — "
            f"{escape(t['employee_name'])}, {db.STATUS_LABELS.get(t['status'], t['status'])}{deadline}"
        )
    await m.answer("\n".join(lines))


@dp.message(Command("task"))
async def cmd_task(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    task_id, _, error = _parse_task_update_args(_command_tail(m))
    if error:
        await m.answer("Укажите ID задачи: <code>/task 12</code>")
        return
    task = await _admin_task_or_answer(m, task_id)
    if not task:
        return
    await m.answer(_admin_task_card(task))


@dp.message(Command("edit"))
async def cmd_edit_task(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        return
    task_id, title, error = _parse_task_update_args(_command_tail(m))
    if error:
        await m.answer("Формат: <code>/edit 12 новый текст задачи</code>")
        return
    task = await _admin_task_or_answer(m, task_id)
    if not task:
        return
    if not title:
        await state.set_state(EditTask.title)
        await state.update_data(task_id=task_id)
        await m.answer(f"✍️ Введите новый текст задачи {db.task_identifier(task_id)}:")
        return
    await _apply_task_title_update(m, task_id, title)


@dp.message(Command("append"))
async def cmd_append_task(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        return
    task_id, addition, error = _parse_task_update_args(_command_tail(m))
    if error:
        await m.answer("Формат: <code>/append 12 уточнение или дополнение</code>")
        return
    task = await _admin_task_or_answer(m, task_id)
    if not task:
        return
    if not addition:
        await state.set_state(EditTask.addition)
        await state.update_data(task_id=task_id)
        await m.answer(f"➕ Введите дополнение к задаче {db.task_identifier(task_id)}:")
        return
    await _apply_task_append(m, task_id, addition)


@dp.message(Command("deadline"))
async def cmd_deadline_task(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        return
    task_id, deadline, error = _parse_task_update_args(_command_tail(m))
    if error:
        await m.answer("Формат: <code>/deadline 12 до пятницы 18:00</code>")
        return
    task = await _admin_task_or_answer(m, task_id)
    if not task:
        return
    if not deadline:
        await state.set_state(EditTask.deadline)
        await state.update_data(task_id=task_id)
        await m.answer(
            f"📅 Введите новый дедлайн для задачи {db.task_identifier(task_id)}. "
            "Чтобы убрать дедлайн, отправьте <code>-</code>."
        )
        return
    await _apply_task_deadline_update(m, task_id, deadline)


@dp.message(EditTask.title)
async def edit_task_title_text(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    await _apply_task_title_update(m, int(data["task_id"]), m.text.strip())


@dp.message(EditTask.addition)
async def append_task_text(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    await _apply_task_append(m, int(data["task_id"]), m.text.strip())


@dp.message(EditTask.deadline)
async def deadline_task_text(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    await _apply_task_deadline_update(m, int(data["task_id"]), m.text.strip())


@dp.message(Command("new"))
async def cmd_new(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        await m.answer("Команда доступна только руководителю.")
        return
    emps = db.list_employees()
    if not emps:
        await m.answer("Нет сотрудников. Попросите их нажать /start у бота.")
        return
    await state.set_state(NewTask.pick)
    await state.update_data(selected=set())
    await m.answer("Кому ставим задачу? Отметьте одного или нескольких:",
                   reply_markup=kb.employees_kb(emps, set()))


@dp.callback_query(NewTask.pick, F.data.startswith("pick:"))
async def pick_toggle(c: CallbackQuery, state: FSMContext):
    emp_id = int(c.data.split(":")[1])
    data = await state.get_data()
    selected: set[int] = set(data.get("selected", set()))
    selected.symmetric_difference_update({emp_id})
    await state.update_data(selected=selected)
    await c.message.edit_reply_markup(reply_markup=kb.employees_kb(db.list_employees(), selected))
    await c.answer()


@dp.callback_query(NewTask.pick, F.data == "pick_cancel")
async def pick_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Постановка задачи отменена.")
    await c.answer()


@dp.callback_query(NewTask.pick, F.data == "pick_done")
async def pick_done(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected", set()))
    if not selected:
        await c.answer("Выберите хотя бы одного сотрудника", show_alert=True)
        return
    await state.set_state(NewTask.title)
    names = ", ".join(escape(db.get_employee(i)["full_name"]) for i in selected)
    await c.message.edit_text(f"Получатели: <b>{names}</b>\n\n✍️ Введите текст задачи:")
    await c.answer()


@dp.message(NewTask.title)
async def task_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(NewTask.deadline)
    await m.answer("📅 Укажите дедлайн (например «до пятницы» или дату). "
                   "Или нажмите «Пропустить».", reply_markup=kb.skip_kb())


@dp.callback_query(NewTask.deadline, F.data == "skip")
async def task_deadline_skip(c: CallbackQuery, state: FSMContext):
    await _finalize_task(c.message, state, deadline="", author_id=c.from_user.id)
    await c.answer()


@dp.message(NewTask.deadline)
async def task_deadline(m: Message, state: FSMContext):
    await _finalize_task(m, state, deadline=m.text.strip(), author_id=m.from_user.id)


async def _finalize_task(msg: Message, state: FSMContext, deadline: str, author_id: int):
    data = await state.get_data()
    selected = set(data.get("selected", set()))
    title = data.get("title", "")
    await state.clear()

    created = 0
    created_lines = []
    for emp_id in sorted(selected):
        task_id = db.create_task(emp_id, author_id, title, deadline=deadline)
        emp = db.get_employee(emp_id)
        created_lines.append(f"• {db.task_identifier(task_id)} — {escape(emp['full_name'])}")
        try:
            await bot.send_message(
                emp_id,
                _task_card(db.get_task(task_id)),
                reply_markup=kb.task_status_kb(task_id),
            )
            created += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось отправить сотруднику %s: %s", emp_id, e)
            await msg.answer(f"⚠️ Не доставлено сотруднику {emp_id} (не нажал /start у бота?).")

    _safe_sync()
    await msg.answer(
        f"✅ Задачи созданы: {len(created_lines)}. Доставлено: {created}.\n"
        + "\n".join(created_lines)
    )


# ---------- Статусы и отчёты (сотрудник) ----------
@dp.callback_query(F.data.startswith("st:"))
async def change_status(c: CallbackQuery):
    _, task_id_s, status = c.data.split(":")
    task_id = int(task_id_s)
    task = db.get_task(task_id)
    if not task or task["employee_id"] != c.from_user.id:
        await c.answer("Это не ваша задача.", show_alert=True)
        return
    db.set_status(task_id, status, c.from_user.id)
    _safe_sync()
    await c.message.edit_text(_task_card(db.get_task(task_id)),
                              reply_markup=kb.task_status_kb(task_id))
    await c.answer(f"Статус: {db.STATUS_LABELS[status]}")
    # уведомить руководителей
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"🔔 {db.task_identifier(task_id)} {escape(task['title'])} → {db.STATUS_LABELS[status]} "
                f"(сотрудник: {escape(db.get_employee(c.from_user.id)['full_name'])})",
            )
        except Exception:  # noqa: BLE001
            pass


@dp.callback_query(F.data.startswith("rep:"))
async def report_start(c: CallbackQuery, state: FSMContext):
    task_id = int(c.data.split(":")[1])
    task = db.get_task(task_id)
    if not task or task["employee_id"] != c.from_user.id:
        await c.answer("Это не ваша задача.", show_alert=True)
        return
    await state.set_state(ReportFSM.text)
    await state.update_data(task_id=task_id)
    await c.message.answer(f"📝 Напишите отчёт по задаче «{escape(task['title'])}»:")
    await c.answer()


@dp.message(ReportFSM.text)
async def report_save(m: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    db.add_report(task_id, m.from_user.id, m.text.strip())
    await state.clear()
    _safe_sync()
    await m.answer("Спасибо, отчёт сохранён ✅")
    task = db.get_task(task_id)
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"📝 Отчёт по {db.task_identifier(task_id)} «{escape(task['title'])}» от "
                f"{escape(db.get_employee(m.from_user.id)['full_name'])}:\n{escape(m.text.strip())}",
            )
        except Exception:  # noqa: BLE001
            pass


# ---------- Отчёты / sync (руководитель) ----------
@dp.message(Command("status"))
async def cmd_status(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    text, _, _ = reports.build_weekly()
    await m.answer(text)


@dp.message(Command("report"))
async def cmd_report(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    text = reports.export_weekly_to_sheets()
    await m.answer(text)
    await m.answer("Выгружено в Google Sheets." if config.SHEETS_ENABLED else
                   "Google Sheets выключен (SHEETS_ENABLED=0).")


@dp.message(Command("sync"))
async def cmd_sync(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    ok = sheets.sync_all_tasks()
    if ok:
        await m.answer("✅ Выгружено в Google Sheets.")
    else:
        await m.answer("⚠️ Не удалось выгрузить.\nПричина: " + (sheets.last_error or "?"))


@dp.message(Command("update"))
async def cmd_update(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    await m.answer("⏳ Обновляюсь: git pull + зависимости + перезапуск…")
    try:
        pull = subprocess.run(["git", "-C", "/opt/taskbot", "pull", "--ff-only"],
                              capture_output=True, text=True, timeout=90)
        out = (pull.stdout + pull.stderr).strip()
        subprocess.run(["/opt/taskbot/venv/bin/pip", "install", "-q", "-r",
                        "/opt/taskbot/requirements.txt"], timeout=240)
        await m.answer(f"✅ Код обновлён:\n{out[:300]}\nПерезапускаю…")
    except Exception as e:  # noqa: BLE001
        await m.answer(f"⚠️ Ошибка обновления: {e}")
        return
    subprocess.Popen(["systemctl", "restart", "taskbot"])


@dp.message(Command("table"))
async def cmd_table(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    await m.answer(
        "📊 <b>Реестр задач (Google Sheets):</b>\n"
        f"{config.SHEET_URL}\n\n"
        "Доступ к таблице — только у тех, кому вы дали его в настройках «Поделиться».",
        disable_web_page_preview=True,
    )


# ---------- Вспомогательное ----------
def _command_tail(m: Message) -> str:
    parts = (m.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _parse_task_update_args(raw: str) -> tuple[int | None, str, str]:
    if not raw:
        return None, "", "empty"
    parts = raw.split(maxsplit=1)
    raw_id = parts[0].removeprefix("#")
    try:
        task_id = int(raw_id)
    except ValueError:
        return None, "", "bad_id"
    if task_id <= 0:
        return None, "", "bad_id"
    value = parts[1].strip() if len(parts) > 1 else ""
    return task_id, value, ""


async def _admin_task_or_answer(m: Message, task_id: int):
    task = db.get_task(task_id)
    if not task:
        await m.answer(f"Не нашёл задачу {db.task_identifier(task_id)}.")
        return None
    return task


async def _apply_task_title_update(m: Message, task_id: int, title: str):
    title = title.strip()
    if not title:
        await m.answer("Текст задачи не может быть пустым.")
        return
    if not db.update_task_title(task_id, title, m.from_user.id):
        await m.answer(f"Не нашёл задачу {db.task_identifier(task_id)}.")
        return
    await _after_task_updated(m, task_id, "Текст задачи обновлён")


async def _apply_task_append(m: Message, task_id: int, addition: str):
    addition = addition.strip()
    if not addition:
        await m.answer("Дополнение не может быть пустым.")
        return
    if not db.append_task_description(task_id, addition, m.from_user.id):
        await m.answer(f"Не нашёл задачу {db.task_identifier(task_id)}.")
        return
    await _after_task_updated(m, task_id, "Дополнение добавлено")


async def _apply_task_deadline_update(m: Message, task_id: int, deadline: str):
    deadline = _normalize_deadline(deadline)
    if not db.update_task_deadline(task_id, deadline, m.from_user.id):
        await m.answer(f"Не нашёл задачу {db.task_identifier(task_id)}.")
        return
    text = "Дедлайн обновлён" if deadline else "Дедлайн убран"
    await _after_task_updated(m, task_id, text)


def _normalize_deadline(deadline: str) -> str:
    value = deadline.strip()
    if value.lower() in {"-", "нет", "убрать", "без дедлайна", "clear", "none"}:
        return ""
    return value


async def _after_task_updated(m: Message, task_id: int, result_text: str):
    _safe_sync()
    task = db.get_task(task_id)
    await m.answer(f"✅ {result_text}.\n\n{_admin_task_card(task)}")
    await _notify_employee_task_updated(m, task)


async def _notify_employee_task_updated(m: Message, task):
    try:
        await bot.send_message(
            task["employee_id"],
            f"✏️ Обновление задачи {db.task_identifier(task['id'])}:\n\n{_task_card(task)}",
            reply_markup=kb.task_status_kb(task["id"]),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось отправить обновление по задаче %s: %s", task["id"], e)
        await m.answer(
            f"⚠️ Не удалось отправить обновление сотруднику {task['employee_id']} "
            "(возможно, он не нажал /start у бота)."
        )


def _admin_task_card(t) -> str:
    employee = db.get_employee(t["employee_id"])
    employee_text = escape(employee["full_name"]) if employee else str(t["employee_id"])
    username = f" (@{escape(employee['username'])})" if employee and employee["username"] else ""
    return (
        f"{_task_card(t)}\n\n"
        f"👤 Сотрудник: {employee_text}{username}\n"
        f"🕒 Создана: {escape(t['created_at'])}\n"
        f"🕒 Обновлена: {escape(t['updated_at'])}"
    )


def _task_card(t) -> str:
    dl = f"\n📅 Дедлайн: {escape(t['deadline'])}" if t["deadline"] else ""
    description = f"\n\n🧾 Описание:\n{escape(t['description'])}" if t["description"] else ""
    return (
        f"📌 <b>Задача {db.task_identifier(t['id'])}</b>\n"
        f"{escape(t['title'])}{description}{dl}\n\n"
        f"Статус: {db.STATUS_LABELS.get(t['status'], t['status'])}"
    )


def _safe_sync():
    try:
        sheets.sync_all_tasks()
    except Exception as e:  # noqa: BLE001
        log.warning("sync failed: %s", e)


async def _weekly_job():
    text = reports.export_weekly_to_sheets()
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(admin, text)
        except Exception as e:  # noqa: BLE001
            log.warning("weekly to %s failed: %s", admin, e)


def _setup_scheduler():
    sched = AsyncIOScheduler(timezone=config.TIMEZONE)
    hour, minute = config.WEEKLY_REPORT_TIME.split(":")
    sched.add_job(_weekly_job, "cron", day_of_week=config.WEEKLY_REPORT_DAY,
                  hour=int(hour), minute=int(minute))
    sched.start()
    log.info("Планировщик: отчёт %s в %s (%s)",
             config.WEEKLY_REPORT_DAY, config.WEEKLY_REPORT_TIME, config.TIMEZONE)


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполните .env")
    db.init_db()
    _setup_scheduler()
    log.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

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
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    MenuButtonCommands,
    Message,
)
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

EMPLOYEE_COMMANDS = [
    BotCommand(command="start", description="Зарегистрироваться или открыть помощь"),
    BotCommand(command="my", description="Мои открытые задачи"),
]

ADMIN_COMMANDS = [
    BotCommand(command="new", description="Поставить задачу"),
    BotCommand(command="tasks", description="Открытые задачи с ID"),
    BotCommand(command="task", description="Выбрать задачу и действие"),
    BotCommand(command="edit", description="Отредактировать задачу"),
    BotCommand(command="append", description="Дополнить задачу"),
    BotCommand(command="deadline", description="Изменить дедлайн"),
    BotCommand(command="status", description="Недельная сводка"),
    BotCommand(command="report", description="Собрать недельный отчёт"),
    BotCommand(command="employees", description="Список сотрудников"),
    BotCommand(command="sync", description="Выгрузить в Google Sheets"),
    BotCommand(command="table", description="Ссылка на таблицу"),
    BotCommand(command="update", description="Обновить код на сервере"),
]


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
            "/task — выбрать задачу и действие\n"
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
    tasks = _open_admin_tasks()
    if not tasks:
        await m.answer("Открытых задач нет.")
        return
    task_blocks = []
    for t in tasks:
        deadline = escape(t["deadline"]) if t["deadline"] else "не указан"
        task_blocks.append(
            f"<b>{db.task_identifier(t['id'])}</b> {escape(t['title'])}\n"
            f"👤 Сотрудник: {escape(t['employee_name'])}\n"
            f"📌 Статус: {db.STATUS_LABELS.get(t['status'], t['status'])}\n"
            f"📅 Дедлайн: {deadline}"
        )
    await m.answer("<b>Открытые задачи:</b>\n\n" + "\n\n".join(task_blocks))


@dp.message(Command("task"))
async def cmd_task(m: Message):
    if not config.is_admin(m.from_user.id):
        return
    raw = _command_tail(m)
    if not raw:
        await _send_admin_task_picker(m)
        return
    task_id, _, error = _parse_task_update_args(raw)
    if error:
        await m.answer("Укажите ID задачи: <code>/task 12</code> или отправьте <code>/task</code> для выбора.")
        return
    task = await _admin_task_or_answer(m, task_id)
    if not task:
        return
    await m.answer(_admin_task_card(task), reply_markup=kb.admin_task_actions_kb(task_id, task["status"]))


@dp.callback_query(F.data.startswith("adm_task:"))
async def admin_task_selected(c: CallbackQuery):
    if not config.is_admin(c.from_user.id):
        await c.answer("Команда доступна только руководителю.", show_alert=True)
        return
    task_id = int(c.data.split(":")[1])
    task = db.get_task(task_id)
    if not task:
        await c.answer("Задача не найдена.", show_alert=True)
        return
    await c.message.edit_text(_admin_task_card(task), reply_markup=kb.admin_task_actions_kb(task_id, task["status"]))
    await c.answer()


@dp.callback_query(F.data.startswith("admact:"))
async def admin_task_action(c: CallbackQuery, state: FSMContext):
    if not config.is_admin(c.from_user.id):
        await c.answer("Команда доступна только руководителю.", show_alert=True)
        return
    _, task_id_s, action = c.data.split(":")
    task_id = int(task_id_s)
    task = db.get_task(task_id)
    if not task:
        await c.answer("Задача не найдена.", show_alert=True)
        return
    if task["status"] == db.STATUS_CANCELLED and action not in {"back", "view", "cancel_no"}:
        await c.message.edit_text(_admin_task_card(task), reply_markup=kb.admin_task_actions_kb(task_id, task["status"]))
        await c.answer("Задача уже отменена.", show_alert=True)
        return
    if action == "back":
        await _edit_admin_task_picker(c)
    elif action == "view":
        await c.message.edit_text(_admin_task_card(task), reply_markup=kb.admin_task_actions_kb(task_id, task["status"]))
    elif action == "cancel":
        await c.message.edit_text(
            f"{_admin_task_card(task)}\n\n"
            "⚠️ Отменить задачу? Она пропадёт из открытых списков, "
            "сотрудник получит уведомление, а в таблице останется со статусом «Отменена».",
            reply_markup=kb.admin_task_cancel_confirm_kb(task_id),
        )
    elif action == "cancel_no":
        await c.message.edit_text(_admin_task_card(task), reply_markup=kb.admin_task_actions_kb(task_id, task["status"]))
    elif action == "cancel_yes":
        await _apply_task_cancel_from_callback(c, task_id)
    elif action == "edit":
        await state.set_state(EditTask.title)
        await state.update_data(task_id=task_id)
        await c.message.edit_text(
            f"✏️ Отправьте новый основной текст задачи {db.task_identifier(task_id)}.\n\n"
            "Старый основной текст будет заменён полностью."
        )
    elif action == "append":
        await state.set_state(EditTask.addition)
        await state.update_data(task_id=task_id)
        await c.message.edit_text(
            f"➕ Отправьте уточнение к задаче {db.task_identifier(task_id)}.\n\n"
            "Старый текст останется, уточнение добавится в описание отдельной записью."
        )
    elif action == "deadline":
        await state.set_state(EditTask.deadline)
        await state.update_data(task_id=task_id)
        await c.message.edit_text(
            f"📅 Отправьте новый дедлайн для задачи {db.task_identifier(task_id)}.\n\n"
            "Чтобы убрать дедлайн, отправьте <code>-</code>."
        )
    else:
        await c.answer("Неизвестное действие.", show_alert=True)
        return
    await c.answer()


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
    await _apply_task_title_update(m, int(data["task_id"]), (m.text or "").strip())


@dp.message(EditTask.addition)
async def append_task_text(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    await _apply_task_append(m, int(data["task_id"]), (m.text or "").strip())


@dp.message(EditTask.deadline)
async def deadline_task_text(m: Message, state: FSMContext):
    if not config.is_admin(m.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    await _apply_task_deadline_update(m, int(data["task_id"]), (m.text or "").strip())


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
@dp.callback_query(NewTask.pick, F.data == "new_cancel")
@dp.callback_query(NewTask.title, F.data == "new_cancel")
@dp.callback_query(NewTask.deadline, F.data == "new_cancel")
async def new_task_cancel(c: CallbackQuery, state: FSMContext):
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
    await c.message.edit_text(
        f"Получатели: <b>{names}</b>\n\n✍️ Введите текст задачи:",
        reply_markup=kb.cancel_task_creation_kb(),
    )
    await c.answer()


@dp.message(NewTask.title)
async def task_title(m: Message, state: FSMContext):
    title = (m.text or "").strip()
    if not title:
        await m.answer("Текст задачи не может быть пустым.", reply_markup=kb.cancel_task_creation_kb())
        return
    await state.update_data(title=title)
    await state.set_state(NewTask.deadline)
    await m.answer("📅 Укажите дедлайн (например «до пятницы» или дату). "
                   "Или нажмите «Пропустить».", reply_markup=kb.skip_kb())


@dp.callback_query(NewTask.deadline, F.data == "skip")
async def task_deadline_skip(c: CallbackQuery, state: FSMContext):
    await _finalize_task(c.message, state, deadline="", author_id=c.from_user.id)
    await c.answer()


@dp.message(NewTask.deadline)
async def task_deadline(m: Message, state: FSMContext):
    deadline = (m.text or "").strip()
    if not deadline:
        await m.answer("Введите дедлайн текстом или нажмите «Пропустить».", reply_markup=kb.skip_kb())
        return
    await _finalize_task(m, state, deadline=deadline, author_id=m.from_user.id)


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
    if task["status"] == db.STATUS_CANCELLED:
        await c.message.edit_text(_task_card(task))
        await c.answer("Задача отменена руководителем.", show_alert=True)
        return
    if status not in db.STATUS_LABELS or status == db.STATUS_CANCELLED:
        await c.answer("Неизвестный статус.", show_alert=True)
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
    if task["status"] == db.STATUS_CANCELLED:
        await c.message.edit_text(_task_card(task))
        await c.answer("Задача отменена руководителем.", show_alert=True)
        return
    await state.set_state(ReportFSM.text)
    await state.update_data(task_id=task_id)
    await c.message.answer(f"📝 Напишите отчёт по задаче «{escape(task['title'])}»:")
    await c.answer()


@dp.message(ReportFSM.text)
async def report_save(m: Message, state: FSMContext):
    data = await state.get_data()
    task_id = data["task_id"]
    task = db.get_task(task_id)
    if task and task["status"] == db.STATUS_CANCELLED:
        await state.clear()
        await m.answer("Отчёт не сохранён: задача отменена руководителем.")
        return
    db.add_report(task_id, m.from_user.id, m.text.strip())
    await state.clear()
    _safe_sync()
    await m.answer("Спасибо, отчёт сохранён ✅")
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
def _open_admin_tasks():
    return [t for t in db.all_tasks() if t["status"] in db.OPEN_STATUSES]


async def _send_admin_task_picker(m: Message):
    tasks = _open_admin_tasks()
    if not tasks:
        await m.answer("Открытых задач нет.")
        return
    await m.answer(
        "<b>Выберите задачу:</b>\n\n"
        "После выбора появятся кнопки действий.",
        reply_markup=kb.admin_tasks_kb(tasks),
    )


async def _edit_admin_task_picker(c: CallbackQuery):
    tasks = _open_admin_tasks()
    if not tasks:
        await c.message.edit_text("Открытых задач нет.")
        return
    await c.message.edit_text(
        "<b>Выберите задачу:</b>\n\n"
        "После выбора появятся кнопки действий.",
        reply_markup=kb.admin_tasks_kb(tasks),
    )


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
    if await _answer_if_task_cancelled(m, task_id):
        return
    title = title.strip()
    if not title:
        await m.answer("Текст задачи не может быть пустым.")
        return
    if not db.update_task_title(task_id, title, m.from_user.id):
        await m.answer(f"Не нашёл задачу {db.task_identifier(task_id)}.")
        return
    await _after_task_updated(m, task_id, "Текст задачи обновлён")


async def _apply_task_append(m: Message, task_id: int, addition: str):
    if await _answer_if_task_cancelled(m, task_id):
        return
    addition = addition.strip()
    if not addition:
        await m.answer("Дополнение не может быть пустым.")
        return
    if not db.append_task_description(task_id, addition, m.from_user.id):
        await m.answer(f"Не нашёл задачу {db.task_identifier(task_id)}.")
        return
    await _after_task_updated(m, task_id, "Дополнение добавлено")


async def _apply_task_deadline_update(m: Message, task_id: int, deadline: str):
    if await _answer_if_task_cancelled(m, task_id):
        return
    deadline = _normalize_deadline(deadline)
    if not db.update_task_deadline(task_id, deadline, m.from_user.id):
        await m.answer(f"Не нашёл задачу {db.task_identifier(task_id)}.")
        return
    text = "Дедлайн обновлён" if deadline else "Дедлайн убран"
    await _after_task_updated(m, task_id, text)


async def _answer_if_task_cancelled(m: Message, task_id: int) -> bool:
    task = db.get_task(task_id)
    if task and task["status"] == db.STATUS_CANCELLED:
        await m.answer(f"Задача {db.task_identifier(task_id)} уже отменена.\n\n{_admin_task_card(task)}")
        return True
    return False


async def _apply_task_cancel_from_callback(c: CallbackQuery, task_id: int):
    if not db.cancel_task(task_id, c.from_user.id):
        await c.answer("Задача не найдена.", show_alert=True)
        return
    _safe_sync()
    task = db.get_task(task_id)
    await c.message.edit_text(
        f"✅ Задача отменена.\n\n{_admin_task_card(task)}",
        reply_markup=kb.admin_task_actions_kb(task_id, task["status"]),
    )
    await _notify_employee_task_updated(c.message, task)


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
    if task["status"] == db.STATUS_CANCELLED:
        text = f"🚫 Задача {db.task_identifier(task['id'])} отменена руководителем:\n\n{_task_card(task)}"
        reply_markup = None
    else:
        text = f"✏️ Обновление задачи {db.task_identifier(task['id'])}:\n\n{_task_card(task)}"
        reply_markup = kb.task_status_kb(task["id"])
    try:
        await bot.send_message(
            task["employee_id"],
            text,
            reply_markup=reply_markup,
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


async def _setup_bot_commands():
    await bot.set_my_commands(EMPLOYEE_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            await bot.set_chat_menu_button(chat_id=admin_id, menu_button=MenuButtonCommands())
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось обновить меню команд для админа %s: %s", admin_id, e)


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполните .env")
    db.init_db()
    await _setup_bot_commands()
    _setup_scheduler()
    log.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

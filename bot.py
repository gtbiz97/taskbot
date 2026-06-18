"""Telegram-бот: постановка задач, статусы, отчёты, недельная сводка.

Запуск:  python bot.py
"""
import asyncio
import logging

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


# ================= СОТРУДНИК =================
@dp.message(CommandStart())
async def cmd_start(m: Message):
    db.upsert_employee(m.from_user.id, m.from_user.username or "", m.from_user.full_name)
    if config.is_admin(m.from_user.id):
        await m.answer(
            "👋 Вы вошли как <b>руководитель</b>.\n\n"
            "/new — поставить задачу\n"
            "/status — открытые задачи\n"
            "/report — собрать недельный отчёт сейчас\n"
            "/employees — список сотрудников\n"
            "/sync — выгрузить в Google Sheets"
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
        uname = f" (@{e['username']})" if e["username"] else ""
        lines.append(f"• {e['full_name']}{uname}")
    await m.answer("\n".join(lines))


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
    names = ", ".join(db.get_employee(i)["full_name"] for i in selected)
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
    for emp_id in selected:
        task_id = db.create_task(emp_id, author_id, title, deadline=deadline)
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
    await msg.answer(f"✅ Задача создана и отправлена ({created}).")


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
                f"🔔 {task['title']} → {db.STATUS_LABELS[status]} "
                f"(сотрудник: {db.get_employee(c.from_user.id)['full_name']})",
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
    await c.message.answer(f"📝 Напишите отчёт по задаче «{task['title']}»:")
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
                f"📝 Отчёт по «{task['title']}» от "
                f"{db.get_employee(m.from_user.id)['full_name']}:\n{m.text.strip()}",
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
    await m.answer("✅ Выгружено в Google Sheets." if ok else
                   "Google Sheets недоступен или выключен.")


# ---------- Вспомогательное ----------
def _task_card(t) -> str:
    dl = f"\n📅 Дедлайн: {t['deadline']}" if t["deadline"] else ""
    return (f"📌 <b>Задача #{t['id']}</b>\n{t['title']}{dl}\n\n"
            f"Статус: {db.STATUS_LABELS.get(t['status'], t['status'])}")


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

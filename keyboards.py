"""Inline-клавиатуры."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db


def _clip(text: str, limit: int = 42) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def employees_kb(employees, selected: set[int]) -> InlineKeyboardMarkup:
    """Мультивыбор сотрудников при постановке задачи."""
    kb = InlineKeyboardBuilder()
    for e in employees:
        mark = "☑️ " if e["tg_id"] in selected else "▫️ "
        kb.button(text=f"{mark}{e['full_name']}", callback_data=f"pick:{e['tg_id']}")
    kb.button(text="➡️ Далее", callback_data="pick_done")
    kb.button(text="✖️ Отмена", callback_data="pick_cancel")
    kb.adjust(1)
    return kb.as_markup()


def task_status_kb(task_id: int) -> InlineKeyboardMarkup:
    """Кнопки статусов в сообщении сотруднику."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Взял", callback_data=f"st:{task_id}:{db.STATUS_ACCEPTED}")
    kb.button(text="🟡 В работе", callback_data=f"st:{task_id}:{db.STATUS_PROGRESS}")
    kb.button(text="✅ Сделано", callback_data=f"st:{task_id}:{db.STATUS_DONE}")
    kb.button(text="❌ Не успел", callback_data=f"st:{task_id}:{db.STATUS_FAILED}")
    kb.button(text="📝 Отчёт", callback_data=f"rep:{task_id}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def admin_tasks_kb(tasks) -> InlineKeyboardMarkup:
    """Список задач для выбора руководителем."""
    kb = InlineKeyboardBuilder()
    for t in tasks:
        label = f"{db.task_identifier(t['id'])} {_clip(t['title'])}"
        kb.button(text=label, callback_data=f"adm_task:{t['id']}")
    kb.adjust(1)
    return kb.as_markup()


def admin_task_actions_kb(task_id: int) -> InlineKeyboardMarkup:
    """Действия руководителя с выбранной задачей."""
    kb = InlineKeyboardBuilder()
    kb.button(text="👁 Показать", callback_data=f"admact:{task_id}:view")
    kb.button(text="✏️ Заменить текст", callback_data=f"admact:{task_id}:edit")
    kb.button(text="➕ Добавить уточнение", callback_data=f"admact:{task_id}:append")
    kb.button(text="📅 Изменить дедлайн", callback_data=f"admact:{task_id}:deadline")
    kb.button(text="↩️ К списку", callback_data=f"admact:{task_id}:back")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def cancel_task_creation_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="new_cancel")]]
    )


def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="skip")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="new_cancel")],
        ]
    )

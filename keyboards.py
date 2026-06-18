"""Inline-клавиатуры."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db


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


def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip")]]
    )

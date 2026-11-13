from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class UserPromoState(StatesGroup):
    waiting_for_code = State()


class AdminSearchState(StatesGroup):
    waiting_for_query = State()
    waiting_for_days_delta = State()
    waiting_for_balance_delta = State()
    waiting_for_limit_ip = State()


class AdminMassCompensateState(StatesGroup):
    waiting_for_days = State()


class AdminPlanState(StatesGroup):
    waiting_for_new_price = State()


class AdminPromoCreateState(StatesGroup):
    waiting_for_params = State()


class AdminBroadcastState(StatesGroup):
    waiting_for_segment = State()
    waiting_for_content = State()
    waiting_for_button = State()
    waiting_for_confirm = State()

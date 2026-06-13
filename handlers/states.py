from aiogram.fsm.state import State, StatesGroup


class PagerConnect(StatesGroup):
    email = State()
    password = State()
    cookies = State()

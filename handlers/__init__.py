from aiogram import Router

from handlers import pager_account, settings


def setup_routers() -> Router:
    root = Router()
    root.include_router(settings.router)
    root.include_router(pager_account.router)
    return root

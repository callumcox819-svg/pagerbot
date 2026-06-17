from aiogram import Router

from handlers import folders, pager_account, settings


def setup_routers() -> Router:
    root = Router()
    root.include_router(settings.router)
    root.include_router(pager_account.router)
    root.include_router(folders.router)
    return root

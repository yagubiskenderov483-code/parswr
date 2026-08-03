from aiogram import Router

from bot.handlers.menu import router as menu_router


def setup_routers() -> Router:
    root = Router()
    root.include_router(menu_router)
    return root

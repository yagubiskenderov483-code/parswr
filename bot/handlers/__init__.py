from aiogram import Router

from bot.handlers.auth import router as auth_router
from bot.handlers.menu import router as menu_router


def setup_routers() -> Router:
    root = Router()
    # Auth first — FSM states take priority
    root.include_router(auth_router)
    root.include_router(menu_router)
    return root

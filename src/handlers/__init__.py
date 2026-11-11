"""Handlers package for VibeRouter Bot."""
from aiogram import Router
from src.handlers.admin import admin_router
from src.handlers.inbox import inbox_router
from src.handlers.group import group_router

main_router = Router()
main_router.include_router(admin_router)
main_router.include_router(inbox_router)
main_router.include_router(group_router)

__all__ = ["main_router", "admin_router", "inbox_router", "group_router"]

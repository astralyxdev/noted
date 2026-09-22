"""Сборка роутеров приложения."""

from api.routes.live import router as live_router
from api.routes.tasks import router as tasks_router

__all__ = ["live_router", "tasks_router"]

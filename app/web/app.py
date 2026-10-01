"""FastAPI app factory."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.web.admin import router as admin_router
from app.web.player import router as player_router


def create_app() -> FastAPI:
    app = FastAPI(title="Moovidex Player", docs_url=None, redoc_url=None)
    app.include_router(player_router)
    app.include_router(admin_router)

    @app.get("/health")
    async def health():
        return JSONResponse({"ok": True})

    return app

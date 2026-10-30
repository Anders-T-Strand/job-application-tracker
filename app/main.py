"""FastAPI app wiring: migrations on startup, background poller, dashboard routes."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.config import Settings, load_settings
from app.discovery.poll import discover_forever
from app.poller import poll_forever
from app.web.discovery import router as discovery_router
from app.web.routes import router
from app.web.tailor import router as tailor_router
from app.web.templating import STATIC_DIR

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    resolved = settings or load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = db.connect(resolved.db_path)
        ran = db.migrate(conn)
        conn.close()
        if ran:
            log.info("applied migrations: %s", ", ".join(ran))
        tasks = []
        if resolved.poll_enabled:
            tasks.append(asyncio.create_task(poll_forever(resolved)))
        if resolved.discovery_enabled:
            tasks.append(asyncio.create_task(discover_forever(resolved)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Job Application Tracker", lifespan=lifespan)
    app.state.settings = resolved
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    app.include_router(discovery_router)
    app.include_router(tailor_router)
    return app


app = create_app()

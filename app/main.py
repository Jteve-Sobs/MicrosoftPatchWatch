import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import init_db
from app.refresh_service import run_all_fetchers
from app.routers import api, web
from app.scheduler import start_scheduler, stop_scheduler

settings = get_settings()
logging.basicConfig(
    level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    if settings.fetch_on_startup:
        asyncio.create_task(run_all_fetchers(trigger="startup"))
    yield
    stop_scheduler()


app = FastAPI(title="WindowsPatchWatch", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(web.router)
app.include_router(api.router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

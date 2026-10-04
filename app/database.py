from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


async def init_db() -> None:
    # Simple create-all for v1 — no schema migrations yet, see README "Bekannte
    # Grenzen". Fine as long as the schema doesn't need to evolve on an existing
    # deployment; switch to Alembic before making breaking model changes.
    from app import models  # noqa: F401  (ensure models are registered on Base)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all never adds columns to an existing table. Postgres-only:
        # the SQLite test DB is always created fresh, with every column.
        if conn.dialect.name == "postgresql":
            await conn.execute(
                text("ALTER TABLE fetch_run_sources ADD COLUMN IF NOT EXISTS new_patch_list TEXT")
            )

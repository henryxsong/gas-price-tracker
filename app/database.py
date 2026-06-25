from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings

engine = create_async_engine(settings.database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    from models import User, Station, GasPrice, AppSetting, UserSetting  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)


async def _migrate(conn):
    """Apply schema changes that create_all won't handle on existing tables."""
    for stmt in [
        "ALTER TABLE stations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
        "ALTER TABLE stations ADD COLUMN latitude REAL",
        "ALTER TABLE stations ADD COLUMN longitude REAL",
    ]:
        try:
            await conn.execute(text(stmt))
        except Exception:
            pass  # column already exists

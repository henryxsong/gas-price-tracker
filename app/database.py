from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
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
    try:
        await conn.execute(
            text("ALTER TABLE stations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        )
    except Exception:
        pass  # column already exists

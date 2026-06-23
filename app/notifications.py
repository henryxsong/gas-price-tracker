import httpx
import json
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from models import GasPrice, Station, User, UserSetting

FUEL_EMOJI = {
    "regular": "⛽",
    "midgrade": "🟡",
    "premium": "🔵",
    "diesel": "🟤",
    "e85": "🌽",
}


async def _get_settings(db: AsyncSession, user: User) -> dict:
    await db.refresh(user, ["setting"])
    if user.setting:
        return json.loads(user.setting.value)
    return {}


async def send_ntfy(
    server_url: str,
    topic: str,
    title: str,
    message: str,
    priority: str = "default",
    token: str | None = None,
) -> None:
    if not topic:
        raise ValueError("NTFY topic is not configured")

    url = f"{server_url.rstrip('/')}/{topic}"
    headers: dict[str, str] = {
        "Title": title,
        "Priority": priority,
        "Tags": "fuelpump",
        "Content-Type": "text/plain",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, content=message.encode(), headers=headers)
        resp.raise_for_status()


async def _latest_prices(db: AsyncSession, user_id: int) -> list[tuple[str, str, float, datetime]]:
    """Return (station_name, fuel_type, price, fetched_at) for the most recent entry per station+fuel for a user."""
    subq = (
        select(
            GasPrice.station_id,
            GasPrice.fuel_type,
            func.max(GasPrice.fetched_at).label("max_at"),
        )
        .join(Station, GasPrice.station_id == Station.id)
        .where(Station.user_id == user_id)
        .group_by(GasPrice.station_id, GasPrice.fuel_type)
        .subquery()
    )
    q = (
        select(Station.name, GasPrice.fuel_type, GasPrice.price, GasPrice.fetched_at)
        .join(Station, GasPrice.station_id == Station.id)
        .join(
            subq,
            (GasPrice.station_id == subq.c.station_id)
            & (GasPrice.fuel_type == subq.c.fuel_type)
            & (GasPrice.fetched_at == subq.c.max_at),
        )
        .where(Station.user_id == user_id, Station.enabled == True)
        .order_by(GasPrice.fuel_type, GasPrice.price)
    )
    rows = await db.execute(q)
    return rows.all()


async def send_daily_summary(db: AsyncSession, user: User) -> None:
    settings = await _get_settings(db, user)
    if not settings.get("notifications_enabled"):
        return

    prices = await _latest_prices(db, user.id)
    if not prices:
        return

    best: dict[str, tuple[str, float]] = {}
    for station_name, fuel_type, price, _ in prices:
        if fuel_type not in best or price < best[fuel_type][1]:
            best[fuel_type] = (station_name, price)

    lines = ["Today's Best Gas Prices\n"]
    for fuel_type in ("regular", "midgrade", "premium", "diesel", "e85"):
        if fuel_type in best:
            station, price = best[fuel_type]
            emoji = FUEL_EMOJI.get(fuel_type, "⛽")
            lines.append(f"{emoji} {fuel_type.title()}: ${price:.3f} @ {station}")

    lines.append(f"\nUpdated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    await send_ntfy(
        server_url=settings.get("ntfy_server_url", "https://ntfy.sh"),
        topic=settings.get("ntfy_topic", ""),
        title="Gas Price Update",
        message="\n".join(lines),
        token=settings.get("ntfy_token") or None,
    )


async def check_and_notify_thresholds(db: AsyncSession, user: User) -> None:
    settings = await _get_settings(db, user)
    if not settings.get("notifications_enabled"):
        return

    thresholds: list[dict] = settings.get("price_thresholds", [])
    if not thresholds:
        return

    prices = await _latest_prices(db, user.id)
    alerts: list[str] = []

    for threshold in thresholds:
        fuel_type = threshold.get("fuel_type")
        limit = threshold.get("price")
        if not fuel_type or limit is None:
            continue
        for station_name, ft, price, _ in prices:
            if ft == fuel_type and price <= limit:
                emoji = FUEL_EMOJI.get(fuel_type, "⛽")
                alerts.append(
                    f"{emoji} {fuel_type.title()} dropped to ${price:.3f} @ {station_name} "
                    f"(threshold: ${limit:.3f})"
                )

    if not alerts:
        return

    await send_ntfy(
        server_url=settings.get("ntfy_server_url", "https://ntfy.sh"),
        topic=settings.get("ntfy_topic", ""),
        title="Gas Price Alert",
        message="Price threshold reached!\n\n" + "\n".join(alerts),
        priority="high",
        token=settings.get("ntfy_token") or None,
    )

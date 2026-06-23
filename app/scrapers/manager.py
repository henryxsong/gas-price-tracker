import asyncio
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from models import Station, GasPrice
from .base import PriceResult, StationSearchResult
from .tulalip import TulalipScraper
from .gasbuddy import GasBuddyScraper
from .costco import CostcoScraper


_tulalip = TulalipScraper()
_gasbuddy = GasBuddyScraper()
_costco = CostcoScraper()

SCRAPER_MAP = {
    "tulalip": _tulalip,
    "gasbuddy": _gasbuddy,
    "costco": _costco,
}


async def search_stations(query: str, source: str = "gasbuddy") -> list[StationSearchResult]:
    scraper = SCRAPER_MAP.get(source, _gasbuddy)
    return await scraper.search_stations(query)


async def refresh_station(station: Station) -> tuple[list[PriceResult], str | None]:
    """Fetch fresh prices for a single station. Returns (results, error_or_None)."""
    scraper = SCRAPER_MAP.get(station.type)
    if not scraper:
        return [], f"Unknown station type: {station.type}"
    try:
        results = await scraper.fetch_prices(station)
        return results, None
    except Exception as exc:
        return [], str(exc)


async def refresh_all(db: AsyncSession, user_id: int | None = None) -> list[dict]:
    """Refresh prices for all enabled stations. Pass user_id to restrict to one user."""
    q = select(Station).where(Station.enabled == True)
    if user_id is not None:
        q = q.where(Station.user_id == user_id)
    result = await db.execute(q)
    stations = result.scalars().all()

    statuses = []
    tasks = [refresh_station(s) for s in stations]
    outcomes = await asyncio.gather(*tasks, return_exceptions=False)

    now = datetime.utcnow()
    for station, (prices, error) in zip(stations, outcomes):
        if prices:
            for pr in prices:
                db.add(GasPrice(
                    station_id=station.id,
                    fuel_type=pr.fuel_type,
                    price=pr.price,
                    fetched_at=now,
                ))
            statuses.append({
                "station_name": station.name,
                "success": True,
                "prices_updated": len(prices),
                "error": None,
            })
        else:
            statuses.append({
                "station_name": station.name,
                "success": False,
                "prices_updated": 0,
                "error": error,
            })

    await db.commit()
    return statuses

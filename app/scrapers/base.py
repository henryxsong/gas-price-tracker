from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PriceResult:
    fuel_type: str  # regular, midgrade, premium, diesel, e85
    price: float


@dataclass
class StationSearchResult:
    external_id: str
    name: str
    address: str
    type: str
    prices: dict[str, Optional[float]] = field(default_factory=dict)


class BaseScraper:
    async def fetch_prices(self, station) -> list[PriceResult]:
        raise NotImplementedError

    async def search_stations(self, query: str) -> list[StationSearchResult]:
        return []

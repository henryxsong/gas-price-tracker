import re
import httpx
from .base import BaseScraper, PriceResult, StationSearchResult

COSTCO_API_BASE = "https://ecom-api.costco.com/core/warehouse-locator/v1"

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "client-identifier": "7c71124c-7bf1-44db-bc9d-498584cd66e5",
    "Origin": "https://www.costco.com",
    "Referer": "https://www.costco.com/",
    "Accept": "application/json",
}

GAS_PRICE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.costco.com/",
    "client-identifier": "7c71124c-7bf1-44db-bc9d-498584cd66e5",
    "Origin": "https://www.costco.com",
}

FUEL_MAP = {
    "regular": "regular",
    "premium": "premium",
    "diesel": "diesel",
    "e85": "e85",
    "midgrade": "midgrade",
    "mid-grade": "midgrade",
    "midpremium": "midgrade",
    "zeroethanol": "regular",
}


class CostcoScraper(BaseScraper):
    async def _warehouse_info(self, warehouse_id: str) -> dict:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                f"{COSTCO_API_BASE}/salesLocations/{warehouse_id}.json",
                headers=API_HEADERS,
            )
            r.raise_for_status()
            return r.json().get("salesLocation", {})

    async def search_stations(self, query: str) -> list[StationSearchResult]:
        warehouse_id = re.sub(r"[^\d]", "", query).lstrip("0")
        if not warehouse_id:
            return []
        try:
            info = await self._warehouse_info(warehouse_id)
        except Exception:
            return []
        if not info:
            return []

        name = next(
            (n["value"] for n in info.get("name", []) if n.get("localeCode") == "en-US"),
            "Costco",
        )
        addr = info.get("address", {})
        address = ", ".join(filter(None, [
            addr.get("line1"), addr.get("city"), addr.get("territory"), addr.get("postalCode"),
        ]))
        has_gas = any(s.get("code") == "gas" for s in info.get("services", []))
        if not has_gas:
            return []

        return [StationSearchResult(
            external_id=warehouse_id,
            name=f"Costco {name}",
            address=address,
            type="costco",
            prices={},
        )]

    async def fetch_prices(self, station) -> list[PriceResult]:
        warehouse_id = station.external_id
        if not warehouse_id:
            return []

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                "https://www.costco.com/AjaxGetGasPricesService",
                params={"warehouseid": warehouse_id},
                headers=GAS_PRICE_HEADERS,
            )
            r.raise_for_status()
            data = r.json()

        prices_raw = data.get(warehouse_id, data.get(str(warehouse_id), {}))
        results: list[PriceResult] = []
        seen: set[str] = set()
        for raw_type, raw_price in prices_raw.items():
            fuel_type = FUEL_MAP.get(raw_type.lower().replace("-", "").replace(" ", ""))
            if not fuel_type or fuel_type in seen:
                continue
            try:
                results.append(PriceResult(fuel_type=fuel_type, price=float(raw_price)))
                seen.add(fuel_type)
            except (ValueError, TypeError):
                continue

        return results

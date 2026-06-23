"""
GasBuddy scraper using their public GraphQL API.

Station IDs are GasBuddy's internal integer IDs.  To find an ID for a new
Costco warehouse (or any station) call search_stations() with a query like
"Costco Issaquah WA" — the results include the ID that you pass to StationCreate.
"""

import httpx
from typing import Optional
from .base import BaseScraper, PriceResult, StationSearchResult

GRAPHQL_URL = "https://www.gasbuddy.com/graphql"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.gasbuddy.com",
    "Referer": "https://www.gasbuddy.com/",
}

LOCATION_SEARCH_QUERY = """
query LocationBySearchTerm($q: String, $cursor: String) {
  locationBySearchTerm(q: $q, cursor: $cursor) {
    stations {
      results {
        id
        name
        address {
          line1
          city
          state
          zip
        }
        latitude
        longitude
        prices {
          credit {
            nickname
            postedTime
            formattedPrice
          }
        }
      }
    }
  }
}
"""

GET_STATION_QUERY = """
query GetStation($id: ID!) {
  station(id: $id) {
    id
    name
    address {
      line1
      city
      state
    }
    prices {
      credit {
        nickname
        postedTime
        formattedPrice
      }
    }
  }
}
"""

# Map GasBuddy fuel nicknames to our internal fuel types
NICKNAME_MAP: dict[str, str] = {
    "regular": "regular",
    "reg": "regular",
    "unleaded": "regular",
    "87": "regular",
    "midgrade": "midgrade",
    "mid": "midgrade",
    "89": "midgrade",
    "plus": "midgrade",
    "premium": "premium",
    "super": "premium",
    "super premium": "premium",
    "91": "premium",
    "93": "premium",
    "diesel": "diesel",
    "dsl": "diesel",
    "e85": "e85",
}


def _parse_price(formatted: str) -> Optional[float]:
    """Convert GasBuddy's formattedPrice string (e.g. '$4.899') to float."""
    try:
        return float(formatted.replace("$", "").strip())
    except (ValueError, AttributeError):
        return None


def _map_fuel(nickname: str) -> Optional[str]:
    return NICKNAME_MAP.get(nickname.lower().strip())


def _format_address(addr: dict) -> str:
    parts = [addr.get("line1", ""), addr.get("city", ""), addr.get("state", ""), addr.get("zip", "")]
    return ", ".join(p for p in parts if p)


def _extract_prices(prices_data: list[dict]) -> dict[str, Optional[float]]:
    result: dict[str, Optional[float]] = {}
    for item in prices_data:
        credit = item.get("credit") or {}
        nickname = credit.get("nickname", "")
        fuel_type = _map_fuel(nickname)
        formatted = credit.get("formattedPrice")
        if fuel_type and formatted:
            price = _parse_price(formatted)
            if price:
                result[fuel_type] = price
    return result


class GasBuddyScraper(BaseScraper):
    async def _graphql(self, client: httpx.AsyncClient, operation: str, variables: dict, query: str) -> dict:
        payload = {"operationName": operation, "variables": variables, "query": query}
        resp = await client.post(GRAPHQL_URL, json=payload, headers=HEADERS)
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            raise RuntimeError(f"GasBuddy GraphQL errors: {body['errors']}")
        return body.get("data", {})

    async def search_stations(self, query: str) -> list[StationSearchResult]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Warm up cookies
            await client.get("https://www.gasbuddy.com/", headers=HEADERS)

            data = await self._graphql(
                client,
                "LocationBySearchTerm",
                {"q": query},
                LOCATION_SEARCH_QUERY,
            )

        results: list[StationSearchResult] = []
        stations = (
            data.get("locationBySearchTerm", {})
            .get("stations", {})
            .get("results", [])
        )
        for s in stations:
            prices = _extract_prices(s.get("prices", []))
            results.append(
                StationSearchResult(
                    external_id=str(s["id"]),
                    name=s["name"],
                    address=_format_address(s.get("address", {})),
                    type="gasbuddy",
                    prices=prices,
                )
            )
        return results

    async def fetch_prices(self, station) -> list[PriceResult]:
        if not station.external_id:
            raise ValueError(f"Station {station.name} has no external_id")

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            await client.get("https://www.gasbuddy.com/", headers=HEADERS)

            data = await self._graphql(
                client,
                "GetStation",
                {"id": station.external_id},
                GET_STATION_QUERY,
            )

        station_data = data.get("station", {})
        prices_raw = station_data.get("prices", [])
        prices = _extract_prices(prices_raw)
        return [PriceResult(fuel_type=ft, price=p) for ft, p in prices.items() if p is not None]

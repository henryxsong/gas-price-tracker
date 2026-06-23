import html as html_module
import re
import httpx
from .base import BaseScraper, PriceResult

TULALIP_URL = "https://www.tulalipmarket.com/"

FUEL_TYPE_MAP = {
    "REG": "regular",
    "REGULAR": "regular",
    "PLS": "midgrade",
    "PLUS": "midgrade",
    "MID": "midgrade",
    "MIDGRADE": "midgrade",
    "SUP": "premium",
    "SUPER": "premium",
    "PREMIUM": "premium",
    "DSL": "diesel",
    "DIESEL": "diesel",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

# Matches patterns like "$4.89910REG." or "$5.159 PLS" or "4.359DSL"
PRICE_RE = re.compile(
    r"\$?(\d+\.\d{2,5})\s*"
    r"(REG|PLS|SUP|DSL|REGULAR|PLUS|SUPER|DIESEL|PREMIUM|MID|MIDGRADE)\b",
    re.IGNORECASE,
)


def _parse_price(price_str: str) -> float:
    """
    Handle the traditional 9/10-cent notation rendered as text.
    e.g. "4.89910" → 4.899  (the trailing "10" is the fraction denominator)
    Plain decimals like "4.599" are returned as-is.
    """
    m = re.match(r"^(\d+\.\d{2})(\d)10$", price_str)
    if m:
        base = float(m.group(1))
        frac = int(m.group(2))
        return round(base + frac / 1000, 4)
    return float(price_str)


class TulalipScraper(BaseScraper):
    async def fetch_prices(self, station) -> list[PriceResult]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(TULALIP_URL, headers=HEADERS)
            response.raise_for_status()

        results: list[PriceResult] = []
        seen: set[str] = set()

        # Strip tags so split spans (Price/FractionUpper/FractionLower/TypeId)
        # concatenate into the "4.89910REG" form the regex expects.
        plain = html_module.unescape(re.sub(r"<[^>]+>", "", response.text))

        for match in PRICE_RE.finditer(plain):
            price_str = match.group(1)
            abbr = match.group(2).upper()
            fuel_type = FUEL_TYPE_MAP.get(abbr)

            if fuel_type and fuel_type not in seen:
                try:
                    price = _parse_price(price_str)
                    results.append(PriceResult(fuel_type=fuel_type, price=price))
                    seen.add(fuel_type)
                except ValueError:
                    continue

        return results

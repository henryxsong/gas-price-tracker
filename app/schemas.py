from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class StationSearchRequest(BaseModel):
    query: str
    source: str = "gasbuddy"


class StationSearchResult(BaseModel):
    external_id: str
    name: str
    address: str
    type: str
    prices: dict[str, float | None] = {}


class StationCreate(BaseModel):
    name: str
    type: str  # tulalip, gasbuddy, costco
    external_id: Optional[str] = None
    address: Optional[str] = None


class PriceOut(BaseModel):
    fuel_type: str
    price: float
    fetched_at: datetime


class StationOut(BaseModel):
    id: int
    name: str
    type: str
    address: Optional[str]
    enabled: bool
    prices: dict[str, float | None] = {}
    last_updated: Optional[datetime] = None

    model_config = {"from_attributes": True}


class BestPrice(BaseModel):
    fuel_type: str
    price: float
    station_name: str
    station_id: int
    fetched_at: datetime


class PriceHistoryPoint(BaseModel):
    fetched_at: datetime
    price: float
    station_name: str
    fuel_type: str


class AppSettingsSchema(BaseModel):
    refresh_enabled: bool = True
    refresh_interval_minutes: int = 60
    ntfy_server_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    notifications_enabled: bool = False
    notification_schedule: str = "0 8 * * *"
    notify_on_refresh: bool = False
    price_thresholds: list[dict] = []
    vehicle_name: str = ""
    vehicle_mpg: float = 0.0
    vehicle_tank_gallons: float = 0.0


class RecommendRequest(BaseModel):
    lat: float
    lng: float
    gas_pct: float
    fuel_type: str = "regular"


class RefreshResult(BaseModel):
    station_name: str
    success: bool
    prices_updated: int
    error: Optional[str] = None

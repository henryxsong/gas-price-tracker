import json
import logging
import math
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

import scheduler as sched
from auth import get_current_user, handle_callback, login_redirect
from config import settings
from database import AsyncSessionLocal, get_db, init_db
from models import GasPrice, Station, User, UserSetting
from notifications import check_and_notify_thresholds, send_daily_summary, send_ntfy
from schemas import (
    AppSettingsSchema,
    RecommendRequest,
    StationCreate,
    StationSearchRequest,
)
from scrapers.manager import refresh_all, refresh_station, search_stations

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = AppSettingsSchema()


# ── Per-user settings helpers ─────────────────────────────────────────────────

async def _load_settings(db: AsyncSession, user: User) -> dict:
    await db.refresh(user, ["setting"])
    if user.setting:
        return json.loads(user.setting.value)
    return DEFAULT_SETTINGS.model_dump()


async def _save_settings(db: AsyncSession, user: User, data: dict) -> None:
    await db.refresh(user, ["setting"])
    if user.setting:
        user.setting.value = json.dumps(data)
    else:
        db.add(UserSetting(user_id=user.id, value=json.dumps(data)))
    await db.commit()


# ── Auth guard helpers ────────────────────────────────────────────────────────

async def _require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Dependency for page routes — redirects to /login if not authenticated."""
    user = await get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user


async def _require_user_api(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Dependency for API routes — returns 401 JSON if not authenticated."""
    user = await get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    async with AsyncSessionLocal() as db:
        # Backfill address on existing Tulalip Market stations that have none
        tulalip_stations = (await db.execute(
            select(Station).where(Station.type == "tulalip", Station.address.is_(None))
        )).scalars().all()
        for st in tulalip_stations:
            st.address = "2832 116th Street NE, Tulalip, WA 98271"
        if tulalip_stations:
            await db.commit()

        # Restore per-user scheduler jobs for all existing users
        users = (await db.execute(select(User))).scalars().all()
        for user in users:
            s = await _load_settings(db, user)
            sched.update_user_refresh_job(user.id, s.get("refresh_interval_minutes", 60), s.get("refresh_enabled", True))
            sched.update_user_notification_job(user.id, s.get("notification_schedule", "0 8 * * *"), s.get("notifications_enabled", False))

    sched.start()
    yield
    sched.stop()


app = FastAPI(title="Gas Price Tracker", lifespan=lifespan)

# SessionMiddleware must be added before @app.middleware("http") decorators
# so that request.session is populated before auth checks run.
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=86400 * 30)

templates = Jinja2Templates(directory="templates")

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _latest_prices_map(db: AsyncSession, user_id: int) -> dict[int, dict]:
    """Return {station_id: {prices: {fuel: price}, last_updated: dt}} for a user's stations."""
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
    rows = await db.execute(
        select(GasPrice.station_id, GasPrice.fuel_type, GasPrice.price, GasPrice.fetched_at)
        .join(
            subq,
            (GasPrice.station_id == subq.c.station_id)
            & (GasPrice.fuel_type == subq.c.fuel_type)
            & (GasPrice.fetched_at == subq.c.max_at),
        )
    )
    result: dict[int, dict] = {}
    for sid, ft, price, fetched_at in rows:
        if sid not in result:
            result[sid] = {"prices": {}, "last_updated": None}
        result[sid]["prices"][ft] = price
        if result[sid]["last_updated"] is None or fetched_at > result[sid]["last_updated"]:
            result[sid]["last_updated"] = fetched_at
    return result


async def _previous_prices_map(db: AsyncSession, user_id: int) -> dict[int, dict[str, float]]:
    """Return {station_id: {fuel_type: price}} for the second-most-recent fetch per station+fuel."""
    subq_latest = (
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
    subq_prev = (
        select(
            GasPrice.station_id,
            GasPrice.fuel_type,
            func.max(GasPrice.fetched_at).label("prev_at"),
        )
        .join(Station, GasPrice.station_id == Station.id)
        .join(
            subq_latest,
            (GasPrice.station_id == subq_latest.c.station_id)
            & (GasPrice.fuel_type == subq_latest.c.fuel_type)
            & (GasPrice.fetched_at < subq_latest.c.max_at),
        )
        .where(Station.user_id == user_id)
        .group_by(GasPrice.station_id, GasPrice.fuel_type)
        .subquery()
    )
    rows = await db.execute(
        select(GasPrice.station_id, GasPrice.fuel_type, GasPrice.price)
        .join(
            subq_prev,
            (GasPrice.station_id == subq_prev.c.station_id)
            & (GasPrice.fuel_type == subq_prev.c.fuel_type)
            & (GasPrice.fetched_at == subq_prev.c.prev_at),
        )
    )
    result: dict[int, dict[str, float]] = {}
    for sid, ft, price in rows:
        if sid not in result:
            result[sid] = {}
        result[sid][ft] = price
    return result


# ── Geo / vehicle helpers ─────────────────────────────────────────────────────

def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def _geocode(address: str) -> tuple[float, float] | None:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": address, "format": "json", "limit": 1},
                headers={"User-Agent": "GasPriceTracker/1.0 (gas-price-tracker)"},
                timeout=8,
            )
            data = r.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


_EPA_BASE = "https://fueleconomy.gov/ws/rest"


async def _fetch_tank_size(vehicle_id: int) -> float | None:
    """Scrape tank size from fueleconomy.gov's comparison page (Edmunds data)."""
    import re
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://www.fueleconomy.gov/feg/Find.do?action=sbs&id={vehicle_id}",
                headers={"User-Agent": "GasPriceTracker/1.0"},
                timeout=10,
            )
        m = re.search(r"Tank Size</th><td[^>]*>([\d.]+)\s*gallons</td>", r.text)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


async def _epa_menu(path: str, params: dict | None = None) -> list[dict]:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_EPA_BASE}/vehicle/menu/{path}",
            params=params or {},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        items = r.json().get("menuItem", [])
        if isinstance(items, dict):
            items = [items]
        return [{"text": i["text"], "value": i["value"]} for i in items]


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: Optional[str] = None):
    if request.session.get("user_id"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.get("/auth/login")
async def auth_login(request: Request):
    return login_redirect(request)


@app.get("/auth/callback")
async def auth_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        response = await handle_callback(request, db)
        # Set up scheduler jobs for the newly logged-in user
        user = await get_current_user(request, db)
        if user:
            s = await _load_settings(db, user)
            sched.update_user_refresh_job(user.id, s.get("refresh_interval_minutes", 60), s.get("refresh_enabled", True))
            sched.update_user_notification_job(user.id, s.get("notification_schedule", "0 8 * * *"), s.get("notifications_enabled", False))

            station_count = (await db.execute(
                select(func.count()).select_from(Station).where(Station.user_id == user.id)
            )).scalar_one()
            if station_count == 0:
                tulalip = Station(user_id=user.id, name="Tulalip Market", type="tulalip", enabled=True, address="2832 116th Street NE, Tulalip, WA 98271")
                db.add(tulalip)
                await db.commit()
                await db.refresh(tulalip)
                try:
                    prices, _ = await refresh_station(tulalip)
                    if prices:
                        now = datetime.utcnow()
                        for pr in prices:
                            db.add(GasPrice(station_id=tulalip.id, fuel_type=pr.fuel_type, price=pr.price, fetched_at=now))
                        await db.commit()
                except Exception:
                    pass

        return response
    except Exception as exc:
        logger.exception("OAuth callback error: %s", exc)
        return RedirectResponse("/login?error=1")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(_require_user)):
    stations_q = await db.execute(
        select(Station).where(Station.user_id == user.id, Station.enabled)
    )
    stations = stations_q.scalars().all()

    latest = await _latest_prices_map(db, user.id)
    previous = await _previous_prices_map(db, user.id)

    stations_out = []
    for s in stations:
        info = latest.get(s.id, {})
        prev = previous.get(s.id, {})
        prices = info.get("prices", {})
        deltas = {
            ft: round(prices[ft] - prev[ft], 3)
            for ft in prices
            if ft in prev
        }
        stations_out.append({
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "address": s.address,
            "prices": prices,
            "deltas": deltas,
            "last_updated": info.get("last_updated"),
        })

    best: dict[str, dict] = {}
    for s in stations_out:
        for ft, price in s["prices"].items():
            if ft not in best or price < best[ft]["price"]:
                best[ft] = {
                    "price": price,
                    "delta": s["deltas"].get(ft),
                    "station_name": s["name"],
                    "station_id": s["id"],
                    "station_address": s["address"],
                }

    fuel_order = ["regular", "midgrade", "premium", "diesel", "e85"]
    best_prices = [{"fuel_type": ft, **best[ft]} for ft in fuel_order if ft in best]

    s_settings = await _load_settings(db, user)
    next_job = sched.get_scheduler().get_job(sched.refresh_job_id(user.id))
    next_refresh = next_job.next_run_time if next_job else None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "stations": stations_out,
            "best_prices": best_prices,
            "fuel_order": fuel_order,
            "next_refresh": next_refresh,
            "refresh_enabled": s_settings.get("refresh_enabled", True),
            "active": "dashboard",
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(_require_user)):
    s = await _load_settings(db, user)
    stations_q = await db.execute(select(Station).where(Station.user_id == user.id))
    stations = stations_q.scalars().all()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "settings": s,
            "stations": [{"id": st.id, "name": st.name, "type": st.type, "address": st.address, "enabled": st.enabled} for st in stations],
            "active": "settings",
        },
    )


# ── API — Stations ────────────────────────────────────────────────────────────

@app.get("/api/stations")
async def api_stations(db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    rows = (await db.execute(select(Station).where(Station.user_id == user.id))).scalars().all()
    latest = await _latest_prices_map(db, user.id)
    return [
        {
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "address": s.address,
            "enabled": s.enabled,
            "prices": latest.get(s.id, {}).get("prices", {}),
            "last_updated": latest.get(s.id, {}).get("last_updated"),
        }
        for s in rows
    ]


@app.post("/api/stations/search")
async def api_search_stations(body: StationSearchRequest, user: User = Depends(_require_user_api)):
    try:
        results = await search_stations(body.query, source=body.source)
        return [
            {"external_id": r.external_id, "name": r.name, "address": r.address, "type": r.type, "prices": r.prices}
            for r in results
        ]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/stations", status_code=201)
async def api_add_station(body: StationCreate, db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    existing = (
        await db.execute(
            select(Station).where(
                Station.user_id == user.id,
                (Station.external_id == body.external_id) if body.external_id else (Station.name == body.name),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Station already exists")

    station = Station(
        user_id=user.id,
        name=body.name,
        type=body.type,
        external_id=body.external_id,
        address=body.address,
        enabled=True,
    )
    db.add(station)
    await db.commit()
    await db.refresh(station)

    try:
        prices, error = await refresh_station(station)
        if prices:
            now = datetime.utcnow()
            for pr in prices:
                db.add(GasPrice(station_id=station.id, fuel_type=pr.fuel_type, price=pr.price, fetched_at=now))
            await db.commit()
    except Exception:
        pass

    return {"id": station.id, "name": station.name}


@app.delete("/api/stations/{station_id}", status_code=204)
async def api_delete_station(station_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    station = (await db.execute(
        select(Station).where(Station.id == station_id, Station.user_id == user.id)
    )).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    await db.delete(station)
    await db.commit()


@app.patch("/api/stations/{station_id}/toggle", status_code=200)
async def api_toggle_station(station_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    station = (await db.execute(
        select(Station).where(Station.id == station_id, Station.user_id == user.id)
    )).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    station.enabled = not station.enabled
    await db.commit()
    return {"enabled": station.enabled}


# ── API — Prices ──────────────────────────────────────────────────────────────

@app.post("/api/refresh")
async def api_refresh_all(db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    statuses = await refresh_all(db, user_id=user.id)
    async with AsyncSessionLocal() as db2:
        fresh_user = (await db2.execute(select(User).where(User.id == user.id))).scalar_one_or_none()
        if fresh_user:
            s = await _load_settings(db2, fresh_user)
            if s.get("notifications_enabled") and s.get("notify_on_refresh"):
                await send_daily_summary(db2, fresh_user)
            await check_and_notify_thresholds(db2, fresh_user)
    return statuses


@app.post("/api/stations/{station_id}/refresh")
async def api_refresh_station(station_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    station = (await db.execute(
        select(Station).where(Station.id == station_id, Station.user_id == user.id)
    )).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    prices, error = await refresh_station(station)
    if prices:
        now = datetime.utcnow()
        for pr in prices:
            db.add(GasPrice(station_id=station.id, fuel_type=pr.fuel_type, price=pr.price, fetched_at=now))
        await db.commit()

    return {"success": bool(prices), "prices_updated": len(prices), "station_name": station.name, "error": error}


@app.get("/api/prices/best")
async def api_best_prices(db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    latest = await _latest_prices_map(db, user.id)
    stations_q = await db.execute(
        select(Station).where(Station.user_id == user.id, Station.enabled)
    )
    stations = {s.id: s for s in stations_q.scalars().all()}

    best: dict[str, dict] = {}
    for sid, info in latest.items():
        station = stations.get(sid)
        if not station:
            continue
        for ft, price in info["prices"].items():
            if ft not in best or price < best[ft]["price"]:
                best[ft] = {"fuel_type": ft, "price": price, "station_name": station.name, "station_id": sid, "fetched_at": info["last_updated"]}

    fuel_order = ["regular", "midgrade", "premium", "diesel", "e85"]
    return [best[ft] for ft in fuel_order if ft in best]


@app.get("/api/prices/history")
async def api_price_history(
    fuel_type: Optional[str] = None,
    station_id: Optional[int] = None,
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_require_user_api),
):
    since = datetime.utcnow() - timedelta(days=days)
    q = (
        select(Station.name, GasPrice.fuel_type, GasPrice.price, GasPrice.fetched_at)
        .join(Station, GasPrice.station_id == Station.id)
        .where(Station.user_id == user.id, GasPrice.fetched_at >= since)
        .order_by(GasPrice.fetched_at)
    )
    if fuel_type:
        q = q.where(GasPrice.fuel_type == fuel_type)
    if station_id:
        q = q.where(GasPrice.station_id == station_id)

    rows = await db.execute(q)
    return [
        {"station_name": name, "fuel_type": ft, "price": price, "fetched_at": fetched_at}
        for name, ft, price, fetched_at in rows
    ]


# ── API — Settings ────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings(db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    return await _load_settings(db, user)


@app.put("/api/settings")
async def api_update_settings(body: AppSettingsSchema, db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    data = body.model_dump()
    await _save_settings(db, user, data)
    sched.update_user_refresh_job(user.id, data["refresh_interval_minutes"], data["refresh_enabled"])
    sched.update_user_notification_job(user.id, data["notification_schedule"], data["notifications_enabled"])
    return {"ok": True}


@app.post("/api/test-notification")
async def api_test_notification(db: AsyncSession = Depends(get_db), user: User = Depends(_require_user_api)):
    s = await _load_settings(db, user)
    try:
        await send_ntfy(
            server_url=s.get("ntfy_server_url", "https://ntfy.sh"),
            topic=s.get("ntfy_topic", ""),
            title="Gas Price Tracker - Test",
            message="If you see this, notifications are working correctly! ⛽",
            token=s.get("ntfy_token") or None,
        )
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── API — Vehicle / EPA lookup ────────────────────────────────────────────────

@app.get("/api/vehicles/years")
async def api_vehicle_years(user: User = Depends(_require_user_api)):
    try:
        return await _epa_menu("year")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/vehicles/makes")
async def api_vehicle_makes(year: str, user: User = Depends(_require_user_api)):
    try:
        return await _epa_menu("make", {"year": year})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/vehicles/models")
async def api_vehicle_models(year: str, make: str, user: User = Depends(_require_user_api)):
    try:
        return await _epa_menu("model", {"year": year, "make": make})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/vehicles/trims")
async def api_vehicle_trims(year: str, make: str, model: str, user: User = Depends(_require_user_api)):
    try:
        return await _epa_menu("options", {"year": year, "make": make, "model": model})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/vehicles/{vehicle_id}/tank")
async def api_vehicle_tank(vehicle_id: int, user: User = Depends(_require_user_api)):
    return {"tank_gallons": await _fetch_tank_size(vehicle_id)}


@app.get("/api/vehicles/{vehicle_id}")
async def api_vehicle_detail(vehicle_id: int, user: User = Depends(_require_user_api)):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{_EPA_BASE}/vehicle/{vehicle_id}",
                headers={"Accept": "application/json"},
                timeout=10,
            )
            d = r.json()
        return {
            "id": d.get("id"),
            "year": d.get("year"),
            "make": d.get("make"),
            "model": d.get("model"),
            "trany": d.get("trany"),
            "mpg_city": d.get("city08"),
            "mpg_highway": d.get("highway08"),
            "mpg_combined": d.get("comb08"),
            "fuel_type": d.get("fuelType1"),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── API — Smart Fill-Up recommendation ────────────────────────────────────────

@app.post("/api/recommend")
async def api_recommend(
    body: RecommendRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_require_user_api),
):
    s = await _load_settings(db, user)
    mpg = float(s.get("vehicle_mpg") or 0)
    tank = float(s.get("vehicle_tank_gallons") or 0)

    if mpg <= 0 or tank <= 0:
        raise HTTPException(
            status_code=400,
            detail="Set your vehicle MPG and tank size in Settings → Vehicle before using Smart Fill-Up.",
        )

    stations_q = await db.execute(
        select(Station).where(Station.user_id == user.id, Station.enabled)
    )
    stations = stations_q.scalars().all()
    latest = await _latest_prices_map(db, user.id)

    gallons_needed = max(0.0, tank * (1 - body.gas_pct / 100))

    results = []
    for station in stations:
        price = latest.get(station.id, {}).get("prices", {}).get(body.fuel_type)
        if price is None:
            continue

        # Lazily geocode and cache coordinates; fall back to name if no address
        if station.latitude is None:
            coords = await _geocode(station.address or station.name)
            if coords:
                station.latitude, station.longitude = coords
                await db.commit()

        if station.latitude is None:
            results.append({
                "station_id": station.id,
                "station_name": station.name,
                "station_address": station.address,
                "distance_miles": None,
                "price": price,
                "gallons_needed": round(gallons_needed, 2),
                "fuel_cost": round(gallons_needed * price, 2),
                "driving_cost": None,
                "total_cost": None,
                "savings_vs_worst": None,
                "has_coordinates": False,
            })
            continue

        # 1.25 road factor converts straight-line to approximate driving distance
        distance = _haversine_miles(body.lat, body.lng, station.latitude, station.longitude) * 1.25
        driving_cost = (distance * 2 / mpg) * price
        fuel_cost = gallons_needed * price
        total_cost = fuel_cost + driving_cost

        results.append({
            "station_id": station.id,
            "station_name": station.name,
            "station_address": station.address,
            "distance_miles": round(distance, 1),
            "price": price,
            "gallons_needed": round(gallons_needed, 2),
            "fuel_cost": round(fuel_cost, 2),
            "driving_cost": round(driving_cost, 2),
            "total_cost": round(total_cost, 2),
            "savings_vs_worst": None,
            "has_coordinates": True,
        })

    with_coords = sorted([r for r in results if r["has_coordinates"]], key=lambda x: x["total_cost"])
    without_coords = [r for r in results if not r["has_coordinates"]]
    ranked = with_coords + without_coords

    for i, r in enumerate(ranked):
        r["rank"] = i + 1

    if len(with_coords) >= 2:
        worst = with_coords[-1]["total_cost"]
        for r in with_coords:
            r["savings_vs_worst"] = round(worst - r["total_cost"], 2)

    return ranked


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

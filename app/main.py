import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import scheduler as sched
from database import AsyncSessionLocal, get_db, init_db
from models import AppSetting, GasPrice, Station
from notifications import check_and_notify_thresholds, send_daily_summary, send_ntfy
from schemas import (
    AppSettingsSchema,
    BestPrice,
    PriceHistoryPoint,
    RefreshResult,
    StationCreate,
    StationOut,
    StationSearchRequest,
    StationSearchResult,
)
from scrapers.manager import refresh_all, refresh_station, search_stations

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = AppSettingsSchema()


async def _load_settings(db: AsyncSession) -> dict:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == "app_settings"))).scalar_one_or_none()
    if row:
        return json.loads(row.value)
    return DEFAULT_SETTINGS.model_dump()


async def _save_settings(db: AsyncSession, data: dict) -> None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == "app_settings"))).scalar_one_or_none()
    if row:
        row.value = json.dumps(data)
    else:
        db.add(AppSetting(key="app_settings", value=json.dumps(data)))
    await db.commit()


async def _setup_default_station(db: AsyncSession) -> None:
    """Add Tulalip Market as the default station on first run."""
    existing = (await db.execute(select(Station).where(Station.type == "tulalip"))).scalar_one_or_none()
    if not existing:
        db.add(Station(
            name="Tulalip Market",
            type="tulalip",
            address="Marysville, WA",
            enabled=True,
        ))
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with AsyncSessionLocal() as db:
        await _setup_default_station(db)
        settings = await _load_settings(db)

    sched.start()
    sched.update_refresh_job(
        settings.get("refresh_interval_minutes", 60),
        settings.get("refresh_enabled", True),
    )
    sched.update_notification_job(
        settings.get("notification_schedule", "0 8 * * *"),
        settings.get("notifications_enabled", False),
    )
    yield
    sched.stop()


app = FastAPI(title="Gas Price Tracker", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _latest_prices_map(db: AsyncSession) -> dict[int, dict[str, float]]:
    """Return {station_id: {fuel_type: price}} using only the most recent fetch per station+fuel."""
    subq = (
        select(
            GasPrice.station_id,
            GasPrice.fuel_type,
            func.max(GasPrice.fetched_at).label("max_at"),
        )
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


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    stations_q = await db.execute(select(Station).where(Station.enabled == True))
    stations = stations_q.scalars().all()

    latest = await _latest_prices_map(db)

    stations_out = []
    for s in stations:
        info = latest.get(s.id, {})
        stations_out.append({
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "address": s.address,
            "prices": info.get("prices", {}),
            "last_updated": info.get("last_updated"),
        })

    # Best price per fuel type
    best: dict[str, dict] = {}
    for s in stations_out:
        for ft, price in s["prices"].items():
            if ft not in best or price < best[ft]["price"]:
                best[ft] = {"price": price, "station_name": s["name"], "station_id": s["id"]}

    fuel_order = ["regular", "midgrade", "premium", "diesel", "e85"]
    best_prices = [
        {"fuel_type": ft, **best[ft]}
        for ft in fuel_order
        if ft in best
    ]

    settings = await _load_settings(db)
    next_job = sched.get_scheduler().get_job(sched.REFRESH_JOB_ID)
    next_refresh = next_job.next_run_time if next_job else None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stations": stations_out,
            "best_prices": best_prices,
            "fuel_order": fuel_order,
            "next_refresh": next_refresh,
            "refresh_enabled": settings.get("refresh_enabled", True),
            "active": "dashboard",
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    settings = await _load_settings(db)
    stations_q = await db.execute(select(Station))
    stations = stations_q.scalars().all()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings,
            "stations": [{"id": s.id, "name": s.name, "type": s.type, "address": s.address, "enabled": s.enabled} for s in stations],
            "active": "settings",
        },
    )


# ── API — Stations ────────────────────────────────────────────────────────────

@app.get("/api/stations")
async def api_stations(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Station))).scalars().all()
    latest = await _latest_prices_map(db)
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
async def api_search_stations(body: StationSearchRequest):
    try:
        results = await search_stations(body.query, source=body.source)
        return [
            {
                "external_id": r.external_id,
                "name": r.name,
                "address": r.address,
                "type": r.type,
                "prices": r.prices,
            }
            for r in results
        ]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/stations", status_code=201)
async def api_add_station(body: StationCreate, db: AsyncSession = Depends(get_db)):
    existing = (
        await db.execute(
            select(Station).where(
                (Station.external_id == body.external_id) if body.external_id else (Station.name == body.name)
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Station already exists")

    station = Station(
        name=body.name,
        type=body.type,
        external_id=body.external_id,
        address=body.address,
        enabled=True,
    )
    db.add(station)
    await db.commit()
    await db.refresh(station)

    # Fetch prices immediately in the background
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
async def api_delete_station(station_id: int, db: AsyncSession = Depends(get_db)):
    station = (await db.execute(select(Station).where(Station.id == station_id))).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    await db.delete(station)
    await db.commit()


@app.patch("/api/stations/{station_id}/toggle", status_code=200)
async def api_toggle_station(station_id: int, db: AsyncSession = Depends(get_db)):
    station = (await db.execute(select(Station).where(Station.id == station_id))).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")
    station.enabled = not station.enabled
    await db.commit()
    return {"enabled": station.enabled}


# ── API — Prices ──────────────────────────────────────────────────────────────

@app.post("/api/refresh")
async def api_refresh_all(db: AsyncSession = Depends(get_db)):
    statuses = await refresh_all(db)
    async with AsyncSessionLocal() as db2:
        settings = await _load_settings(db2)
        if settings.get("notifications_enabled") and settings.get("notify_on_refresh"):
            await send_daily_summary(db2)
        await check_and_notify_thresholds(db2)
    return statuses


@app.post("/api/stations/{station_id}/refresh")
async def api_refresh_station(station_id: int, db: AsyncSession = Depends(get_db)):
    station = (await db.execute(select(Station).where(Station.id == station_id))).scalar_one_or_none()
    if not station:
        raise HTTPException(status_code=404, detail="Station not found")

    prices, error = await refresh_station(station)
    if prices:
        now = datetime.utcnow()
        for pr in prices:
            db.add(GasPrice(station_id=station.id, fuel_type=pr.fuel_type, price=pr.price, fetched_at=now))
        await db.commit()

    return {"success": bool(prices), "prices_updated": len(prices), "error": error}


@app.get("/api/prices/best")
async def api_best_prices(db: AsyncSession = Depends(get_db)):
    latest = await _latest_prices_map(db)
    stations_q = await db.execute(select(Station).where(Station.enabled == True))
    stations = {s.id: s for s in stations_q.scalars().all()}

    best: dict[str, dict] = {}
    for sid, info in latest.items():
        station = stations.get(sid)
        if not station:
            continue
        for ft, price in info["prices"].items():
            if ft not in best or price < best[ft]["price"]:
                best[ft] = {
                    "fuel_type": ft,
                    "price": price,
                    "station_name": station.name,
                    "station_id": sid,
                    "fetched_at": info["last_updated"],
                }

    fuel_order = ["regular", "midgrade", "premium", "diesel", "e85"]
    return [best[ft] for ft in fuel_order if ft in best]


@app.get("/api/prices/history")
async def api_price_history(
    fuel_type: Optional[str] = None,
    station_id: Optional[int] = None,
    days: int = 7,
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=days)
    q = (
        select(Station.name, GasPrice.fuel_type, GasPrice.price, GasPrice.fetched_at)
        .join(Station, GasPrice.station_id == Station.id)
        .where(GasPrice.fetched_at >= since)
        .order_by(GasPrice.fetched_at)
    )
    if fuel_type:
        q = q.where(GasPrice.fuel_type == fuel_type)
    if station_id:
        q = q.where(GasPrice.station_id == station_id)

    rows = await db.execute(q)
    return [
        {
            "station_name": name,
            "fuel_type": ft,
            "price": price,
            "fetched_at": fetched_at,
        }
        for name, ft, price, fetched_at in rows
    ]


# ── API — Settings ────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def api_get_settings(db: AsyncSession = Depends(get_db)):
    return await _load_settings(db)


@app.put("/api/settings")
async def api_update_settings(body: AppSettingsSchema, db: AsyncSession = Depends(get_db)):
    data = body.model_dump()
    await _save_settings(db, data)

    sched.update_refresh_job(data["refresh_interval_minutes"], data["refresh_enabled"])
    sched.update_notification_job(data["notification_schedule"], data["notifications_enabled"])

    return {"ok": True}


@app.post("/api/test-notification")
async def api_test_notification(db: AsyncSession = Depends(get_db)):
    settings = await _load_settings(db)
    try:
        await send_ntfy(
            server_url=settings.get("ntfy_server_url", "https://ntfy.sh"),
            topic=settings.get("ntfy_topic", ""),
            title="Gas Price Tracker - Test",
            message="If you see this, notifications are working correctly! ⛽",
            token=settings.get("ntfy_token") or None,
        )
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

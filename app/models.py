from datetime import datetime
from sqlalchemy import String, Float, Boolean, DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from database import Base


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(50))  # tulalip, gasbuddy, costco
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    prices: Mapped[list["GasPrice"]] = relationship(
        "GasPrice", back_populates="station", cascade="all, delete-orphan"
    )


class GasPrice(Base):
    __tablename__ = "gas_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(Integer, ForeignKey("stations.id", ondelete="CASCADE"))
    fuel_type: Mapped[str] = mapped_column(String(50))  # regular, midgrade, premium, diesel
    price: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    station: Mapped["Station"] = relationship("Station", back_populates="prices")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)

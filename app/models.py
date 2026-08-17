from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ProductFamily(str, enum.Enum):
    WINDOWS_CLIENT = "windows_client"
    WINDOWS_SERVER = "windows_server"
    DOTNET_FRAMEWORK = "dotnet_framework"
    DOTNET = "dotnet"


class Product(Base):
    """A trackable "thing" with a version, e.g. 'Windows 11, version 24H2' or
    '.NET Framework 4.8.1'. Created on the fly by fetchers (get-or-create by key)."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    family: Mapped[str] = mapped_column(String(30), index=True)
    is_ltsc: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    support_end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # True when support is known to have ended but the source didn't give an
    # exact date (see fetchers.base.ProductInfo.support_ended).
    support_ended: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    patches: Mapped[list["Patch"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="desc(Patch.release_date)"
    )


class Patch(Base):
    """One KB / build / release for a product. Rows are never deleted, so the
    full set of rows for a product IS its history; the newest release_date is
    the 'current' patch level."""

    __tablename__ = "patches"
    __table_args__ = (
        UniqueConstraint("product_id", "kb_number", "build", name="uq_patch_product_kb_build"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)

    kb_number: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    build: Mapped[str | None] = mapped_column(String(60), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    update_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    release_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True, index=True)
    severity: Mapped[str | None] = mapped_column(String(30), nullable=True)
    kb_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(60))

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    product: Mapped["Product"] = relationship(back_populates="patches")


class FetchRun(Base):
    """One execution of "check all sources", for the status badge and an audit log."""

    __tablename__ = "fetch_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    trigger: Mapped[str] = mapped_column(String(20), default="scheduler")
    new_patches: Mapped[int] = mapped_column(Integer, default=0)
    updated_products: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

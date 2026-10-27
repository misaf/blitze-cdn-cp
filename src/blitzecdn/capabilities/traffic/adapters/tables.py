"""Hourly counters and the cursors committed with them."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from blitzecdn.core.persistence.tables import Base, UtcDateTime


class TrafficHourRow(Base):
    __tablename__ = "traffic_hours"
    __table_args__ = (
        Index("ix_traffic_hours_hour_hostname_edge", "hour", "hostname", "edge"),
        CheckConstraint(
            "requests > 0 AND bytes_sent >= 0 AND body_bytes_sent >= 0",
            name="traffic_hours_counters_check",
        ),
    )

    edge: Mapped[str] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(primary_key=True)
    hour: Mapped[datetime] = mapped_column(UtcDateTime, primary_key=True)
    requests: Mapped[int] = mapped_column()
    bytes_sent: Mapped[int] = mapped_column()
    body_bytes_sent: Mapped[int] = mapped_column()


class TrafficCursorRow(Base):
    __tablename__ = "traffic_cursors"
    __table_args__ = (
        CheckConstraint("offset > 0", name="traffic_cursors_offset_check"),
    )

    edge: Mapped[str] = mapped_column(primary_key=True)
    stream_id: Mapped[str] = mapped_column(primary_key=True)
    offset: Mapped[int] = mapped_column()
    anchor: Mapped[str] = mapped_column()

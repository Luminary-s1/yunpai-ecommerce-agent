from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True)
class DemandPolicy:
    """The single, replayable definition of the M6-R V1 demand target."""

    policy_version: str = "demand-v1"
    timezone: str = "Asia/Shanghai"
    included_payment_statuses: tuple[str, ...] = (
        "paid",
        "partially_refunded",
        "refunded",
    )
    excluded_order_statuses: tuple[str, ...] = ("canceled",)
    late_arrival_policy: str = "rebuild_fixed_14_day_window"
    rebuild_lookback_days: int = 14

    def evidence(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "timezone": self.timezone,
            "included_payment_statuses": list(self.included_payment_statuses),
            "excluded_order_statuses": list(self.excluded_order_statuses),
            "late_arrival_policy": self.late_arrival_policy,
            "rebuild_lookback_days": self.rebuild_lookback_days,
        }


DEMAND_V1 = DemandPolicy()


class DemandFactRebuild(BaseModel):
    """A deterministic rebuild/backfill request for one store or store/SKU scope."""

    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    sku_id: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["full", "incremental"] = "incremental"
    start_date: date | None = None
    end_date: date | None = None
    coverage_complete: bool = False

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.mode == "full" and (self.start_date is None or self.end_date is None):
            raise ValueError("full_rebuild_window_required")
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("rebuild_window_requires_both_bounds")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("rebuild_window_invalid")
        return self

    def resolved_window(self, *, current_business_date: date) -> tuple[date, date]:
        if self.start_date is not None and self.end_date is not None:
            return self.start_date, self.end_date
        end_date = current_business_date
        return end_date - timedelta(days=DEMAND_V1.rebuild_lookback_days - 1), end_date

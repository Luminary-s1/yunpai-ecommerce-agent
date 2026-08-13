from .engine import (
    PRODUCT_FORECAST_HORIZONS,
    SUPPORTED_FORECAST_MODELS,
    ForecastEngine,
    ForecastPolicy,
)
from .models import DEMAND_V1, DemandFactRebuild, DemandPolicy
from .planning import (
    InventoryPlanningError,
    InventoryPlanningPolicy,
    InventoryPlanningService,
)
from .run_service import ForecastRunError, ForecastRunService

__all__ = [
    "DEMAND_V1",
    "DemandFactRebuild",
    "DemandFactService",
    "DemandPolicy",
    "ForecastEngine",
    "ForecastPolicy",
    "ForecastRunError",
    "ForecastRunService",
    "InventoryPlanningError",
    "InventoryPlanningPolicy",
    "InventoryPlanningService",
    "PRODUCT_FORECAST_HORIZONS",
    "SUPPORTED_FORECAST_MODELS",
]


def __getattr__(name: str):
    if name == "DemandFactService":
        from .service import DemandFactService

        return DemandFactService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

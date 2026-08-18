"""M9-R WP1 读模型骨架：数据结构保证隔离铁律 + 数据准备度。

公开 API 边界：
- 构造：models 各类型 / MetricValue.from_value / MetricValue.missing
- 派生：build_sku_readiness（纯函数）
- 投影：build_read_model_from_manifest（纯内存，无副作用）
- 异常：DataUnavailableError（MISSING fail-fast）
"""
from .errors import DataUnavailableError
from .factory import METRIC_SPECS, build_read_model_from_manifest
from .models import (
    AggregateRule,
    DataTrust,
    Granularity,
    ItemReadModel,
    MetricValue,
    SKUReadModel,
    StoreReadModel,
)
from .readiness import (
    FUNNEL_COMPLETE,
    FUNNEL_FIELDS,
    FUNNEL_PARTIAL,
    FUNNEL_UNAVAILABLE,
    MetricReadiness,
    SKUReadiness,
    build_sku_readiness,
)

__all__ = [
    "AggregateRule",
    "DataTrust",
    "DataUnavailableError",
    "FUNNEL_COMPLETE",
    "FUNNEL_FIELDS",
    "FUNNEL_PARTIAL",
    "FUNNEL_UNAVAILABLE",
    "Granularity",
    "ItemReadModel",
    "METRIC_SPECS",
    "MetricReadiness",
    "MetricValue",
    "SKUReadModel",
    "SKUReadiness",
    "StoreReadModel",
    "build_read_model_from_manifest",
    "build_sku_readiness",
]

"""M9-R 读模型领域错误。

无 I/O、无状态。仅一个异常类：访问 evidence_state=MISSING 的指标值时抛出。
"""


class DataUnavailableError(ValueError):
    """MISSING 指标被读取时抛出，阻断下游计算（fail-fast）。"""

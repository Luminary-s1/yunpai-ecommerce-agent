from __future__ import annotations

from typing import Any, Literal


EVIDENCE_FRESHNESS_VERSION = "evidence-freshness-v1"
EvidenceFreshnessStatus = Literal["current", "stale", "superseded"]


def evidence_freshness(
    *,
    status: EvidenceFreshnessStatus,
    reason_codes: list[str] | tuple[str, ...],
    evidence_ref: dict[str, Any],
    current_ref: dict[str, Any],
    max_age_hours: int | None = None,
) -> dict[str, Any]:
    """Build the single read-side freshness envelope used by evidence products."""

    reasons = list(dict.fromkeys(str(code) for code in reason_codes if str(code)))
    if status == "current" and reasons:
        raise ValueError("current_evidence_cannot_have_stale_reasons")
    if status != "current" and not reasons:
        raise ValueError("non_current_evidence_requires_reason")
    result: dict[str, Any] = {
        "policy_version": EVIDENCE_FRESHNESS_VERSION,
        "status": status,
        "usable_as_current": status == "current",
        "reason_codes": reasons,
        "evidence_ref": dict(evidence_ref),
        "current_ref": dict(current_ref),
    }
    if max_age_hours is not None:
        result["max_age_hours"] = max_age_hours
    return result

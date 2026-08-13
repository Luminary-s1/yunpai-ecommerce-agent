from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from .registry import ConnectorRegistry


SOURCE_PROVENANCE_VERSION = "source-provenance-v1"
SourceType = Literal["virtual", "operational", "mixed", "unknown"]
_COMPLETENESS = {"complete", "partial", "legacy_missing"}


class SourceProvenanceError(ValueError):
    """Raised when frozen source provenance cannot be read safely."""


def unknown_source_provenance(*, basis: str) -> dict[str, Any]:
    return {
        "policy_version": SOURCE_PROVENANCE_VERSION,
        "source_type": "unknown",
        "virtual": False,
        "connectors": [],
        "completeness": "legacy_missing",
        "basis": basis,
    }


def read_source_provenance(
    value: Any,
    *,
    missing_basis: str,
) -> dict[str, Any]:
    """Validate frozen provenance; legacy absence is explicit, never operational."""

    if value is None:
        return unknown_source_provenance(basis=missing_basis)
    if not isinstance(value, dict):
        raise SourceProvenanceError("source_provenance_invalid")
    if value.get("policy_version") != SOURCE_PROVENANCE_VERSION:
        raise SourceProvenanceError("source_provenance_version_unsupported")
    connectors = value.get("connectors")
    if not isinstance(connectors, list):
        raise SourceProvenanceError("source_provenance_invalid")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, bool | None]] = set()
    for item in connectors:
        if not isinstance(item, dict):
            raise SourceProvenanceError("source_provenance_invalid")
        connector_id = item.get("connector_id")
        capability_version = item.get("capability_version")
        virtual = item.get("virtual")
        if not isinstance(connector_id, str) or not connector_id:
            raise SourceProvenanceError("source_provenance_invalid")
        if capability_version is not None and not isinstance(capability_version, str):
            raise SourceProvenanceError("source_provenance_invalid")
        if virtual is not None and not isinstance(virtual, bool):
            raise SourceProvenanceError("source_provenance_invalid")
        key = (connector_id, capability_version, virtual)
        if key in seen:
            raise SourceProvenanceError("source_provenance_invalid")
        seen.add(key)
        normalized.append(
            {
                "connector_id": connector_id,
                "capability_version": capability_version,
                "virtual": virtual,
            }
        )
    normalized.sort(
        key=lambda item: (
            item["connector_id"],
            item["capability_version"] or "",
            str(item["virtual"]),
        )
    )
    completeness = value.get("completeness")
    basis = value.get("basis")
    if completeness not in _COMPLETENESS or not isinstance(basis, str) or not basis:
        raise SourceProvenanceError("source_provenance_invalid")
    expected_type = _source_type(normalized, incomplete=completeness != "complete")
    expected_virtual = expected_type == "virtual"
    if value.get("source_type") != expected_type or value.get("virtual") is not expected_virtual:
        raise SourceProvenanceError("source_provenance_invalid")
    return {
        "policy_version": SOURCE_PROVENANCE_VERSION,
        "source_type": expected_type,
        "virtual": expected_virtual,
        "connectors": normalized,
        "completeness": completeness,
        "basis": basis,
    }


def merge_source_provenance(
    values: Iterable[Any],
    *,
    basis: str,
) -> dict[str, Any]:
    validated = [
        read_source_provenance(value, missing_basis=f"{basis}:missing")
        for value in values
    ]
    connectors: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, bool | None]] = set()
    incomplete = not validated
    for provenance in validated:
        incomplete = incomplete or provenance["completeness"] != "complete"
        for item in provenance["connectors"]:
            key = (
                item["connector_id"],
                item["capability_version"],
                item["virtual"],
            )
            if key not in seen:
                seen.add(key)
                connectors.append(dict(item))
    connectors.sort(
        key=lambda item: (
            item["connector_id"],
            item["capability_version"] or "",
            str(item["virtual"]),
        )
    )
    source_type = _source_type(connectors, incomplete=incomplete)
    return {
        "policy_version": SOURCE_PROVENANCE_VERSION,
        "source_type": source_type,
        "virtual": source_type == "virtual",
        "connectors": connectors,
        "completeness": "partial" if incomplete else "complete",
        "basis": basis,
    }


class SourceProvenanceResolver:
    """Freeze connector capabilities at evidence creation time."""

    def __init__(self, registry: ConnectorRegistry) -> None:
        self.registry = registry

    def freeze(self, connector_ids: Iterable[Any], *, basis: str) -> dict[str, Any]:
        connectors: list[dict[str, Any]] = []
        incomplete = False
        for connector_id in sorted({str(value) for value in connector_ids if value}):
            try:
                capability = self.registry.get(connector_id).capabilities()
            except ValueError:
                incomplete = True
                connectors.append(
                    {
                        "connector_id": connector_id,
                        "capability_version": None,
                        "virtual": None,
                    }
                )
            else:
                connectors.append(
                    {
                        "connector_id": capability.connector_id,
                        "capability_version": capability.capability_version,
                        "virtual": capability.virtual,
                    }
                )
        if not connectors:
            return unknown_source_provenance(basis=basis)
        source_type = _source_type(connectors, incomplete=incomplete)
        return {
            "policy_version": SOURCE_PROVENANCE_VERSION,
            "source_type": source_type,
            "virtual": source_type == "virtual",
            "connectors": connectors,
            "completeness": "partial" if incomplete else "complete",
            "basis": basis,
        }


def _source_type(
    connectors: list[dict[str, Any]],
    *,
    incomplete: bool,
) -> SourceType:
    if incomplete or not connectors or any(item["virtual"] is None for item in connectors):
        return "unknown"
    kinds = {bool(item["virtual"]) for item in connectors}
    if kinds == {True}:
        return "virtual"
    if kinds == {False}:
        return "operational"
    return "mixed"

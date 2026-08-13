from __future__ import annotations

import pytest

from ecommerce_agent.connectors import (
    ConnectorCapabilities,
    ConnectorRegistry,
    SourceProvenanceError,
    SourceProvenanceResolver,
    VirtualTaobaoConnector,
    merge_source_provenance,
    read_source_provenance,
)


class _OperationalConnector:
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            connector_id="operational-test",
            display_name="Operational test connector",
            capability_version="test-v1",
            virtual=False,
            resources=["orders"],
            modes=["read"],
        )


def test_source_provenance_distinguishes_virtual_mixed_and_unknown() -> None:
    registry = ConnectorRegistry()
    registry.register(VirtualTaobaoConnector())
    registry.register(_OperationalConnector())
    resolver = SourceProvenanceResolver(registry)

    virtual = resolver.freeze(["virtual_taobao"], basis="test-input")
    operational = resolver.freeze(["operational-test"], basis="test-input")
    mixed = merge_source_provenance(
        [virtual, operational], basis="combined-test-input"
    )
    unknown = resolver.freeze(["unregistered-connector"], basis="test-input")
    legacy = read_source_provenance(None, missing_basis="legacy-record")

    assert virtual["source_type"] == "virtual" and virtual["virtual"] is True
    assert operational["source_type"] == "operational"
    assert operational["virtual"] is False
    assert mixed["source_type"] == "mixed" and mixed["virtual"] is False
    assert {item["connector_id"] for item in mixed["connectors"]} == {
        "virtual_taobao",
        "operational-test",
    }
    assert unknown["source_type"] == "unknown" and unknown["virtual"] is False
    assert unknown["completeness"] == "partial"
    assert legacy["source_type"] == "unknown" and legacy["virtual"] is False
    assert legacy["completeness"] == "legacy_missing"


def test_source_provenance_rejects_tampered_projection() -> None:
    registry = ConnectorRegistry()
    registry.register(VirtualTaobaoConnector())
    provenance = SourceProvenanceResolver(registry).freeze(
        ["virtual_taobao"], basis="test-input"
    )
    provenance["source_type"] = "operational"

    with pytest.raises(SourceProvenanceError, match="source_provenance_invalid"):
        read_source_provenance(provenance, missing_basis="unused")

from __future__ import annotations

from .base import Connector, ConnectorCapabilities


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        capabilities = connector.capabilities()
        if capabilities.connector_id in self._connectors:
            raise ValueError(f"connector already registered: {capabilities.connector_id}")
        self._connectors[capabilities.connector_id] = connector

    def get(self, connector_id: str) -> Connector:
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise ValueError(f"connector not registered: {connector_id}")
        return connector

    def catalog(self) -> list[ConnectorCapabilities]:
        return [
            self._connectors[key].capabilities()
            for key in sorted(self._connectors)
        ]

    def __len__(self) -> int:
        return len(self._connectors)


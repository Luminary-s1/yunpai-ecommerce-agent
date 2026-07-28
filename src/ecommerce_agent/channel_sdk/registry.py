from __future__ import annotations

from .adapter import ChannelAdapter
from .contracts import (
    CHANNEL_SDK_CONTRACT_VERSION,
    ChannelAdapterError,
    ChannelCapabilityDeclaration,
)


class ChannelAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        declaration = adapter.declaration()
        if declaration.contract_version != CHANNEL_SDK_CONTRACT_VERSION:
            raise ChannelAdapterError(
                f"channel adapter {declaration.platform} declares contract "
                f"{declaration.contract_version}, runtime requires "
                f"{CHANNEL_SDK_CONTRACT_VERSION}",
                kind="capability_unavailable",
                platform=declaration.platform,
            )
        if declaration.platform != adapter.platform:
            raise ChannelAdapterError(
                "channel adapter platform does not match its declaration",
                kind="capability_unavailable",
                platform=adapter.platform,
            )
        if declaration.platform in self._adapters:
            raise ChannelAdapterError(
                f"channel adapter already registered: {declaration.platform}",
                kind="conflict",
                platform=declaration.platform,
            )
        self._adapters[declaration.platform] = adapter

    def get(self, platform: str) -> ChannelAdapter:
        adapter = self._adapters.get(platform)
        if adapter is None:
            raise ChannelAdapterError(
                f"no channel adapter registered for platform: {platform}",
                kind="not_found",
                platform=platform,
            )
        return adapter

    def catalog(self) -> list[ChannelCapabilityDeclaration]:
        return [
            self._adapters[platform].declaration()
            for platform in sorted(self._adapters)
        ]

    def __len__(self) -> int:
        return len(self._adapters)

    def __contains__(self, platform: str) -> bool:
        return platform in self._adapters

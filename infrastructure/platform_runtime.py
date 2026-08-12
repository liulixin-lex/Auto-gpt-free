from __future__ import annotations

from core.registry import list_platforms, load_all
from domain.platforms import PlatformCapabilities, PlatformDescriptor


class PlatformRuntime:
    """Expose the single supported ChatGPT platform to the application layer."""

    def list_platforms(self) -> list[PlatformDescriptor]:
        load_all()
        return [
            PlatformDescriptor(
                name=item["name"],
                display_name=item["display_name"],
                version=item["version"],
                capabilities=PlatformCapabilities(
                    supported_executors=list(item.get("supported_executors", [])),
                    supported_identity_modes=list(
                        item.get("supported_identity_modes", [])
                    ),
                    supported_oauth_providers=list(
                        item.get("supported_oauth_providers", [])
                    ),
                ),
            )
            for item in list_platforms()
        ]

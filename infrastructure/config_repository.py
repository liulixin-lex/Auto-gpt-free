from __future__ import annotations

from core.config_store import config_store
from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository


class ConfigRepository:
    BASE_KEYS = {
        "default_executor",
        "default_identity_provider",
        # CLIProxyAPI / CPA
        "cpa_api_url",
        "cpa_api_key",
        # Sub2API
        "sub2api_base_url",
        "sub2api_token",
        "sub2api_email",
        "sub2api_password",
        # Remote sync target: none | cpa | sub2api | both
        "sync_target",
        "auto_upload_enabled",
        # Continuous probe / cleanup / replenish
        "auto_probe_enabled",
        "auto_probe_interval_minutes",
        "auto_ops_interval_seconds",
        "auto_delete_remote_enabled",
        "auto_delete_local_invalid",
        "auto_sync_delete_enabled",
        "auto_register_enabled",
        "auto_replenish_enabled",
        "auto_replenish_target",
        "auto_register_count",
        "auto_register_concurrency",
        "auto_register_executor",
        # Optional stable egress / CF clearance (Settings → 同步)
        "proxy_runtime_enabled",
        "proxy_runtime_proxy_url",
        "proxy_runtime_scope",
        "proxy_runtime_clearance_mode",
        "proxy_runtime_flaresolverr_url",
        "proxy_runtime_clearance_cookie",
        "proxy_runtime_clearance_ua",
        "proxy_runtime_refresh_interval_sec",
        "proxy_runtime_timeout_sec",
        "proxy_runtime_skip_ssl_verify",
        # runtime status (read-mostly)
        "auto_ops_last_probe_at",
        "auto_ops_last_probe_result",
        "auto_ops_last_replenish_at",
        "auto_ops_last_cycle_at",
        "auto_ops_last_cycle_summary",
        "persistent_task_auto_ops",
    }

    def __init__(self, definitions: ProviderDefinitionsRepository | None = None):
        self.definitions = definitions or ProviderDefinitionsRepository()

    def get_allowed_keys(self) -> set[str]:
        keys = set(self.BASE_KEYS)
        for provider_type in ("mailbox", "captcha"):
            for definition in self.definitions.list_by_type(provider_type, enabled_only=False):
                for field in definition.get_fields():
                    field_key = str(field.get("key") or "").strip()
                    if field_key:
                        keys.add(field_key)
        return keys

    def get_flat(self) -> dict[str, str]:
        data = config_store.get_all()
        allowed = self.get_allowed_keys()
        return {
            key: str(value or "")
            for key, value in data.items()
            if key in allowed
        }

    def update_flat(self, data: dict[str, str]) -> list[str]:
        allowed = self.get_allowed_keys()
        safe = {key: value for key, value in data.items() if key in allowed}
        config_store.set_many(safe)
        return list(safe.keys())

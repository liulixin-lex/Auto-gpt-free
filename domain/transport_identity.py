"""Immutable attempt-scoped network identity for ChatGPT registration."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from domain.registration_runtime import stable_resource_ref


@dataclass(frozen=True, slots=True)
class TransportIdentity:
    egress_id: str
    proxy_lease_id: str
    sticky_session_id: str
    fingerprint_id: str
    user_agent: str
    curl_impersonate: str
    sec_ch_ua: str
    sec_ch_ua_mobile: str
    sec_ch_ua_platform: str
    locale: str
    timezone_id: str
    device_id: str
    sentinel_profile: str

    def apply(self, profile: dict[str, Any]) -> dict[str, Any]:
        out = dict(profile or {})
        out.update(
            {
                "egress_id": self.egress_id,
                "proxy_lease_id": self.proxy_lease_id,
                "sticky_session_id": self.sticky_session_id,
                "fingerprint_id": self.fingerprint_id,
                "user_agent": self.user_agent,
                "impersonate": self.curl_impersonate,
                "sec_ch_ua": self.sec_ch_ua,
                "sec_ch_ua_mobile": self.sec_ch_ua_mobile,
                "sec_ch_ua_platform": self.sec_ch_ua_platform,
                "locale": self.locale,
                "timezone_id": self.timezone_id,
                "device_id": self.device_id,
                "sentinel_profile": self.sentinel_profile,
            }
        )
        return out


def chrome_major(user_agent: str) -> int | None:
    match = re.search(r"(?:Chrome|Chromium)/(\d+)", str(user_agent or ""))
    return int(match.group(1)) if match else None


def build_transport_identity(
    profile: dict[str, Any],
    *,
    proxy_url: str | None,
    device_id: str,
    egress_id: str = "",
) -> TransportIdentity:
    source = dict(profile or {})
    proxy_ref = str(source.get("proxy_lease_id") or stable_resource_ref(proxy_url))
    fingerprint = str(source.get("fingerprint_id") or source.get("key") or "default")
    sticky = str(source.get("sticky_session_id") or proxy_ref)
    return TransportIdentity(
        egress_id=str(egress_id or source.get("egress_id") or proxy_ref or "direct"),
        proxy_lease_id=proxy_ref,
        sticky_session_id=sticky,
        fingerprint_id=fingerprint,
        user_agent=str(source.get("user_agent") or ""),
        curl_impersonate=str(source.get("impersonate") or "chrome142"),
        sec_ch_ua=str(source.get("sec_ch_ua") or ""),
        sec_ch_ua_mobile=str(source.get("sec_ch_ua_mobile") or "?0"),
        sec_ch_ua_platform=str(source.get("sec_ch_ua_platform") or '"Windows"'),
        locale=str(source.get("locale") or "en-US"),
        timezone_id=str(source.get("timezone_id") or "America/New_York"),
        device_id=str(device_id or ""),
        sentinel_profile=str(source.get("sentinel_profile") or fingerprint),
    )

"""Tests for the Camoufox-only browser engine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from platforms.chatgpt.browser_engine import (
    BrowserFailureArtifactCollector,
    CamoufoxEngine,
    build_camoufox_proxy,
)


class _FakeCamoufox:
    last_kwargs = None

    def __init__(self, **kwargs):
        type(self).last_kwargs = dict(kwargs)

    def __enter__(self):
        return "camoufox-browser"

    def __exit__(self, *_args):
        return False


class _ConsoleMessage:
    text = "access_token=secret-token user@example.com"


class _EvidencePage:
    url = "https://auth.openai.com/verify?code=123456&email=user@example.com"

    def __init__(self) -> None:
        self.masked = False

    def on(self, event, callback):
        assert event == "console"
        callback(_ConsoleMessage())

    def evaluate(self, _script, secrets):
        assert "user@example.com" in secrets
        self.masked = True
        return True

    def screenshot(self, *, path, full_page):
        assert self.masked is True
        assert full_page is True
        Path(path).write_bytes(b"redacted-image")

    def content(self):
        return (
            '<input type="password" value="Secret!123">'
            '<script>access_token=secret-token; email="user@example.com"</script>'
        )


def test_proxy_url_is_converted_without_losing_authentication_fields():
    assert build_camoufox_proxy("http://user:pass@127.0.0.1:8080") == {
        "server": "http://127.0.0.1:8080",
        "username": "user",
        "password": "pass",
    }


def test_windows_headless_uses_native_headless_and_attempt_viewport():
    engine = CamoufoxEngine(
        headless=True,
        profile={"locale": "en-GB", "viewport": {"width": 1536, "height": 864}},
        attempt_id="attempt-1",
        camoufox_class=_FakeCamoufox,
        system_name="Windows",
    )

    options = engine.build_launch_options()

    assert options["headless"] is True
    assert options["window"] == (1536, 864)
    assert options["locale"] == "en-GB"


def test_packaged_runtime_uses_bundled_camoufox_assets(monkeypatch, tmp_path):
    from camoufox import geolocation

    monkeypatch.setattr(geolocation, "GEOIP_DIR", geolocation.GEOIP_DIR)
    monkeypatch.setattr(geolocation, "MMDB_DIR", geolocation.MMDB_DIR)
    monkeypatch.setattr(geolocation, "GEOIP_CONFIG", geolocation.GEOIP_CONFIG)
    executable = tmp_path / "camoufox.exe"
    executable.write_bytes(b"fixture")
    (tmp_path / "version.json").write_text('{"version":"152.0.4"}', encoding="utf-8")
    addon = tmp_path / "addons" / "UBO"
    addon.mkdir(parents=True)
    (addon / "manifest.json").write_text("{}", encoding="utf-8")
    geoip = tmp_path / "geoip"
    (geoip / "mmdb").mkdir(parents=True)
    (geoip / "config.yml").write_text("name: fixture", encoding="utf-8")
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(executable))
    monkeypatch.setenv("CAMOUFOX_ADDON_PATH", str(addon))
    monkeypatch.setenv("CAMOUFOX_GEOIP_DIR", str(geoip))

    options = CamoufoxEngine(
        headless=True,
        attempt_id="attempt-packaged",
        camoufox_class=_FakeCamoufox,
        system_name="Windows",
    ).build_launch_options()

    assert options["executable_path"] == str(executable)
    assert options["ff_version"] == 152
    assert options["addons"] == [str(addon)]
    assert options["exclude_addons"]

    assert geolocation.GEOIP_DIR == geoip
    assert geolocation.MMDB_DIR == geoip / "mmdb"


def test_missing_packaged_executable_falls_back_to_camoufox_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(tmp_path / "missing.exe"))

    options = CamoufoxEngine(
        headless=True,
        attempt_id="attempt-cache-fallback",
        camoufox_class=_FakeCamoufox,
        system_name="Windows",
    ).build_launch_options()

    assert "executable_path" not in options


def test_invalid_bundled_version_does_not_override_camoufox_detection(monkeypatch, tmp_path):
    executable = tmp_path / "camoufox.exe"
    executable.write_bytes(b"fixture")
    (tmp_path / "version.json").write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(executable))

    options = CamoufoxEngine(
        headless=True,
        attempt_id="attempt-invalid-version",
        camoufox_class=_FakeCamoufox,
        system_name="Windows",
    ).build_launch_options()

    assert options["executable_path"] == str(executable)
    assert "ff_version" not in options


def test_linux_headless_defaults_to_camoufox_virtual_display(monkeypatch):
    monkeypatch.delenv("CAMOUFOX_LINUX_HEADLESS", raising=False)
    engine = CamoufoxEngine(
        headless=True,
        attempt_id="attempt-linux",
        camoufox_class=_FakeCamoufox,
        system_name="Linux",
    )

    assert engine.build_launch_options()["headless"] == "virtual"


@pytest.mark.parametrize(
    ("configured", "expected"),
    (("native", True), ("display", False)),
)
def test_linux_headless_runtime_can_be_configured(monkeypatch, configured, expected):
    monkeypatch.setenv("CAMOUFOX_LINUX_HEADLESS", configured)
    engine = CamoufoxEngine(
        headless=True,
        attempt_id="attempt-linux-configured",
        camoufox_class=_FakeCamoufox,
        system_name="Linux",
    )

    assert engine.build_launch_options()["headless"] is expected


def test_headed_mode_is_visible_and_proxy_geoip_is_bound_to_same_attempt():
    engine = CamoufoxEngine(
        headless=False,
        proxy="socks5://127.0.0.1:1080",
        attempt_id="attempt-headed",
        camoufox_class=_FakeCamoufox,
        system_name="Windows",
    )

    options = engine.build_launch_options()

    assert options["headless"] is False
    assert options["proxy"] == {"server": "socks5://127.0.0.1:1080"}
    assert options["geoip"] is True
    assert options["block_webrtc"] is True


def test_engine_opens_injected_camoufox_with_built_options():
    engine = CamoufoxEngine(
        headless=False,
        attempt_id="attempt-open",
        camoufox_class=_FakeCamoufox,
        system_name="Windows",
    )

    with engine.open() as browser:
        assert browser == "camoufox-browser"

    assert _FakeCamoufox.last_kwargs["headless"] is False
    assert _FakeCamoufox.last_kwargs["window"]


def test_production_engine_prepares_camoufox_before_open(monkeypatch):
    from platforms.chatgpt import browser_engine
    from services import browser_runtime

    class _ProductionCamoufox(_FakeCamoufox):
        pass

    monkeypatch.setattr(browser_engine, "_Camoufox", _ProductionCamoufox)
    monkeypatch.setattr(browser_runtime, "ensure_camoufox", lambda: Path("camoufox.exe"))
    engine = browser_engine.CamoufoxEngine(
        headless=True,
        attempt_id="attempt-runtime",
        camoufox_class=_ProductionCamoufox,
        system_name="Windows",
    )

    with engine.open() as browser:
        assert browser == "camoufox-browser"


def test_engine_reports_missing_camoufox_at_open_time():
    engine = CamoufoxEngine(
        headless=True,
        attempt_id="attempt-missing",
        camoufox_class=None,
        system_name="Windows",
    )

    with pytest.raises(RuntimeError, match="Camoufox"):
        engine.open()


def test_failure_bundle_masks_page_and_redacts_dom_url_console_and_error(tmp_path):
    collector = BrowserFailureArtifactCollector("attempt/evidence", root=tmp_path)
    page = _EvidencePage()
    collector.observe(page)

    bundle = collector.capture(
        page,
        RuntimeError("password=Secret!123 access_token=secret-token"),
        secrets=("user@example.com", "Secret!123", "secret-token"),
    )

    assert Path(bundle.screenshot).read_bytes() == b"redacted-image"
    dom = Path(bundle.dom).read_text(encoding="utf-8")
    diagnostic = Path(bundle.diagnostic).read_text(encoding="utf-8")
    combined = dom + diagnostic
    assert "Secret!123" not in combined
    assert "secret-token" not in combined
    assert "user@example.com" not in combined
    data = json.loads(diagnostic)
    assert data["url"] == "https://auth.openai.com/verify"
    assert data["files"]["screenshot"] == "screenshot.png"

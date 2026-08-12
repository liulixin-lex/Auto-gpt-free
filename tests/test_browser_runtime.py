from __future__ import annotations

from pathlib import Path

from services import browser_runtime


def test_playwright_cache_uses_persistent_app_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setenv("APP_BROWSER_CACHE_DIR", str(tmp_path / "runtime"))

    target = browser_runtime.configure_playwright_cache()

    assert target == (tmp_path / "runtime" / "playwright").resolve()
    assert target.is_dir()


def test_existing_playwright_runtime_skips_download(monkeypatch, tmp_path):
    executable = tmp_path / "chromium.exe"
    executable.write_bytes(b"fixture")
    calls = []
    monkeypatch.setattr(browser_runtime, "configure_playwright_cache", lambda: tmp_path)
    monkeypatch.setattr(browser_runtime, "_playwright_chromium_executable", lambda: executable)
    monkeypatch.setattr(browser_runtime, "_install_playwright_chromium", lambda: calls.append("install"))

    assert browser_runtime.ensure_playwright_chromium() == executable
    assert calls == []


def test_missing_playwright_runtime_installs_once(monkeypatch, tmp_path):
    executable = tmp_path / "chromium.exe"
    calls = []
    monkeypatch.setenv("APP_BROWSER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(browser_runtime, "configure_playwright_cache", lambda: tmp_path)
    monkeypatch.setattr(browser_runtime, "_playwright_chromium_executable", lambda: executable)

    def install():
        calls.append("install")
        executable.write_bytes(b"fixture")

    monkeypatch.setattr(browser_runtime, "_install_playwright_chromium", install)

    assert browser_runtime.ensure_playwright_chromium() == executable
    assert calls == ["install"]


def test_existing_camoufox_runtime_skips_download(monkeypatch, tmp_path):
    executable = tmp_path / "camoufox.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(browser_runtime, "_camoufox_executable", lambda: executable)

    assert browser_runtime.ensure_camoufox() == executable


def test_missing_camoufox_runtime_installs_once(monkeypatch, tmp_path):
    executable = tmp_path / "camoufox.exe"
    calls = []
    monkeypatch.setenv("APP_BROWSER_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(browser_runtime, "_camoufox_executable", lambda: executable)

    class Fetcher:
        def install(self):
            calls.append("install")
            executable.write_bytes(b"fixture")

    monkeypatch.setattr("camoufox.pkgman.CamoufoxFetcher", Fetcher)

    assert browser_runtime.ensure_camoufox() == executable
    assert browser_runtime.ensure_camoufox() == executable
    assert calls == ["install"]


def test_runtime_lock_recovers_stale_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_BROWSER_CACHE_DIR", str(tmp_path))
    lock_dir = tmp_path / "locks" / "fixture.lock"
    lock_dir.mkdir(parents=True)
    import os

    os.utime(lock_dir, (0, 0))

    with browser_runtime._install_lock("fixture", timeout_seconds=1):
        assert lock_dir.is_dir()

    assert not lock_dir.exists()

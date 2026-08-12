"""Install large browser runtimes on first use and reuse them across upgrades."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Iterator


class BrowserRuntimeInstallError(RuntimeError):
    pass


def runtime_cache_root() -> Path:
    configured = str(os.getenv("APP_BROWSER_CACHE_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".cache" / "auto-gpt-free" / "browser-runtime").resolve()


def configure_playwright_cache() -> Path:
    configured = str(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "") or "").strip()
    target = Path(configured).expanduser().resolve() if configured else runtime_cache_root() / "playwright"
    target.mkdir(parents=True, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(target)
    return target


@contextmanager
def _install_lock(name: str, *, timeout_seconds: float = 1800.0) -> Iterator[None]:
    lock_dir = runtime_cache_root() / "locks" / f"{name}.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock_dir.stat().st_mtime > 3600:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise BrowserRuntimeInstallError(f"等待 {name} 运行时安装超时")
            time.sleep(0.25)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


def _playwright_chromium_executable() -> Path:
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        return Path(playwright.chromium.executable_path)
    finally:
        playwright.stop()


def _install_playwright_chromium() -> None:
    from playwright._impl._driver import compute_driver_executable

    node, cli = compute_driver_executable()
    result = subprocess.run(
        [str(node), str(cli), "install", "chromium"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "download failed").strip()[-1000:]
        raise BrowserRuntimeInstallError(f"Playwright Chromium 下载失败: {detail}")


def ensure_playwright_chromium(executable_path: str | Path | None = None) -> Path:
    configure_playwright_cache()
    executable = Path(executable_path) if executable_path else _playwright_chromium_executable()
    if executable.is_file():
        return executable
    with _install_lock("playwright-chromium"):
        executable = Path(executable_path) if executable_path else _playwright_chromium_executable()
        if not executable.is_file():
            _install_playwright_chromium()
        executable = Path(executable_path) if executable_path else _playwright_chromium_executable()
        if not executable.is_file():
            raise BrowserRuntimeInstallError("Playwright Chromium 安装后仍未找到可执行文件")
    return executable


def _camoufox_executable() -> Path:
    from camoufox.pkgman import camoufox_path

    return Path(camoufox_path(download_if_missing=False))


def ensure_camoufox() -> Path:
    try:
        executable = _camoufox_executable()
        if executable.is_file():
            return executable
    except Exception:
        pass
    with _install_lock("camoufox"):
        try:
            executable = _camoufox_executable()
            if executable.is_file():
                return executable
        except Exception:
            pass
        try:
            from camoufox.pkgman import CamoufoxFetcher

            CamoufoxFetcher().install()
            executable = _camoufox_executable()
        except Exception as exc:
            raise BrowserRuntimeInstallError(f"Camoufox 下载失败: {type(exc).__name__}: {exc}") from exc
        if not executable.is_file():
            raise BrowserRuntimeInstallError("Camoufox 安装后仍未找到可执行文件")
    return executable


__all__ = [
    "BrowserRuntimeInstallError",
    "configure_playwright_cache",
    "ensure_camoufox",
    "ensure_playwright_chromium",
    "runtime_cache_root",
]

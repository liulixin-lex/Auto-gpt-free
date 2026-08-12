from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
I18N = ROOT / "frontend" / "src" / "lib" / "i18n.ts"
SETTINGS_PAGE = ROOT / "frontend" / "src" / "pages" / "SettingsPage.tsx"
SETTINGS = ROOT / "frontend" / "src" / "pages" / "Settings.tsx"
APP = ROOT / "frontend" / "src" / "App.tsx"
ACCOUNTS = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
DASHBOARD = ROOT / "frontend" / "src" / "pages" / "Dashboard.tsx"
ACCOUNT_MODALS = ROOT / "frontend" / "src" / "features" / "accounts" / "modals.tsx"
ACCOUNT_HELPERS = ROOT / "frontend" / "src" / "features" / "accounts" / "helpers.ts"
UPDATE_BANNER = ROOT / "frontend" / "src" / "components" / "UpdateBanner.tsx"


def _has_cjk(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in value)


def _en_messages(source: str) -> str:
    start = source.index("const EN_MESSAGES")
    end = source.index("};", start)
    return source[start:end]


def test_english_catalog_has_no_chinese_leaks():
    source = I18N.read_text(encoding="utf-8")
    english = _en_messages(source)
    assert not _has_cjk(english)
    assert '"sidebar.languageToggle": "Switch to Chinese"' in english
    assert '"language.zh": "Chinese"' in english


def test_primary_surfaces_use_i18n_for_visible_copy():
    for path in (
        SETTINGS_PAGE,
        SETTINGS,
        APP,
        ACCOUNTS,
        DASHBOARD,
        ACCOUNT_MODALS,
        ACCOUNT_HELPERS,
        UPDATE_BANNER,
    ):
        source = path.read_text(encoding="utf-8")
        assert not _has_cjk(source), path


def test_runtime_localizers_do_not_contain_corrupted_placeholders():
    source = I18N.read_text(encoding="utf-8")
    start = source.index("const STAGE_LABELS")
    end = source.index("function hasCjk", start)
    runtime_catalog = source[start:end]
    assert '"??' not in runtime_catalog
    assert "\\u51c6\\u5907" in runtime_catalog

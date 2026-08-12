from pathlib import Path


APP_TSX = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"
TASK_LOG_PANEL = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "components"
    / "tasks"
    / "TaskLogPanel.tsx"
)
JOBS_PAGE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "Jobs.tsx"
TASKS_LIB = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "tasks.ts"
AUTH_MIDDLEWARE = Path(__file__).resolve().parents[1] / "core" / "auth.py"
REGISTRATION_MODAL = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "features"
    / "accounts"
    / "modals.tsx"
)


def _nav_items_block() -> str:
    source = APP_TSX.read_text(encoding="utf-8")
    start = source.index("const NAV_ITEMS: NavItem[] = [")
    end = source.index("];", start)
    return source[start:end]


def test_sidebar_top_level_nav_keeps_dashboard_chatgpt_and_settings():
    block = _nav_items_block()

    assert block.count("path:") == 4
    assert 'path: "/"' in block
    assert 'labelKey: "nav.overview"' in block
    assert 'path: "/accounts/chatgpt"' in block
    assert 'labelKey: "nav.pool"' in block
    assert 'path: "/jobs"' in block
    assert 'path: "/settings"' in block
    assert 'labelKey: "nav.settings"' in block


def test_sidebar_hides_accounts_menu_and_other_business_links():
    source = APP_TSX.read_text(encoding="utf-8")

    assert "setAccountsOpen" not in source
    assert "getPlatforms" not in source
    assert "nav.accounts" not in source
    assert "nav.ctfGptPlus" not in source
    assert "nav.plusManager" not in source
    assert "nav.tasks" not in source


def test_sidebar_exposes_core_settings_items():
    source = APP_TSX.read_text(encoding="utf-8")

    start = source.index("const SETTINGS_NAV_ITEMS:")
    end = source.index("];", start)
    block = source[start:end]

    assert block.count('hash: "') == 4
    assert 'labelKey: "nav.settings.general", hash: "general"' in block
    assert 'labelKey: "nav.settings.mailbox", hash: "mailbox"' in block
    assert 'labelKey: "nav.settings.captcha", hash: "captcha"' in block
    assert 'labelKey: "nav.settings.network", hash: "network"' in block

    assert "/settings?tab=${item.hash}" in source


def test_sse_stream_uses_cookie_without_query_token():
    frontend = TASK_LOG_PANEL.read_text(encoding="utf-8")
    middleware = AUTH_MIDDLEWARE.read_text(encoding="utf-8")

    assert "new EventSource(streamUrl, { withCredentials: true })" in frontend
    assert "?token=" not in frontend
    assert 'request.query_params.get("token")' not in middleware


def test_registration_modal_defaults_to_one_and_preserves_user_override():
    source = REGISTRATION_MODAL.read_text(encoding="utf-8")

    assert "const [concurrency, setConcurrency] = useState(1)" in source
    assert "? 5" not in source
    assert "setConcurrency(recommended)" not in source


def test_jobs_surface_exposes_stop_control_and_cancel_api():
    jobs = JOBS_PAGE.read_text(encoding="utf-8")
    tasks = TASKS_LIB.read_text(encoding="utf-8")

    assert "cancellingTaskId" in jobs
    assert "taskHistory.terminateTitle" in jobs
    assert "stopJob(job.taskId)" in jobs
    assert 'includes(status)' in jobs
    assert 'encodeURIComponent(taskId)' in tasks
    assert '`/tasks/${encodeURIComponent(taskId)}/cancel`' in tasks

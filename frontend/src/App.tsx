import {
  BrowserRouter,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { useEffect, useState, type ReactNode } from "react";
import { getAuthToken, setAuthToken, API, cn } from "@/lib/utils";
import { I18nProvider, useI18n } from "@/lib/i18n-context";
import type { TranslationKey } from "@/lib/i18n";
import Dashboard from "@/pages/Dashboard";
import Accounts from "@/pages/Accounts";
import SettingsPage from "@/pages/SettingsPage";
import Jobs from "@/pages/Jobs";
import UpdateBanner from "@/components/UpdateBanner";
import QqGroupModal from "@/components/QqGroupModal";
import { LiveJobsProvider, useLiveJobs } from "@/lib/live-jobs";
import {
  Gauge,
  Moon,
  Settings as SettingsIcon,
  Sun,
  Monitor,
  Languages,
  Layers,
  Activity,
} from "lucide-react";

type NavItem = {
  path: string;
  labelKey?: TranslationKey;
  label?: string;
  icon: any;
  exact?: boolean;
  code: string;
};

const SETTINGS_NAV_ITEMS: { labelKey: TranslationKey; hash: string }[] = [
  { labelKey: "nav.settings.general", hash: "general" },
  { labelKey: "nav.settings.mailbox", hash: "mailbox" },
  { labelKey: "nav.settings.captcha", hash: "captcha" },
  { labelKey: "nav.settings.sync", hash: "sync" },
];

const NAV_ITEMS: NavItem[] = [
  {
    path: "/",
    labelKey: "nav.overview",
    icon: Gauge,
    exact: true,
    code: "01",
  },
  {
    path: "/accounts/chatgpt",
    labelKey: "nav.pool",
    icon: Layers,
    code: "02",
  },
  {
    path: "/jobs",
    labelKey: "nav.jobs",
    icon: Activity,
    code: "03",
  },
  {
    path: "/settings",
    labelKey: "nav.settings",
    icon: SettingsIcon,
    code: "04",
  },
];

function Deck({
  theme,
  toggleTheme,
}: {
  theme: string;
  toggleTheme: () => void;
}) {
  const { t, toggleLanguage } = useI18n();
  const location = useLocation();
  const { jobs } = useLiveJobs();
  const runningCount = jobs.filter(
    (j) =>
      !j.status ||
      !["succeeded", "failed", "cancelled", "interrupted"].includes(j.status),
  ).length;

  return (
    <header className="xy-deck">
      <div className="xy-mark">
        <div className="xy-mark-box">XY</div>
        <div>
          <div className="xy-mark-name">xyAUTO</div>
          <div className="xy-mark-tag">{t("brand.tag")}</div>
        </div>
      </div>

      <nav className="xy-tabs" aria-label="primary">
        {NAV_ITEMS.map(({ path, labelKey, label: itemLabel, icon: Icon, exact, code }) => {
          const active = exact
            ? location.pathname === path
            : location.pathname.startsWith(path);
          const label = itemLabel || (labelKey ? t(labelKey) : path);
          return (
            <NavLink
              key={path}
              to={path}
              end={exact}
              className={cn("xy-tab", active && "xy-tab-on")}
            >
              <Icon className="h-3.5 w-3.5 opacity-80" strokeWidth={2} />
              <span className="font-[family-name:var(--font-mono)] text-[10px] opacity-60">
                {code}
              </span>
              <span>{label}</span>
              {path === "/jobs" && runningCount > 0 ? (
                <span className="ml-1 inline-flex min-w-[16px] items-center justify-center rounded-full bg-[var(--accent)] px-1 font-[family-name:var(--font-mono)] text-[10px] font-bold text-[oklch(0.14_0_0)]">
                  {runningCount}
                </span>
              ) : null}
            </NavLink>
          );
        })}
      </nav>

      <div className="xy-deck-tools">
        <button
          onClick={toggleTheme}
          className="xy-icon-btn"
          title={
            theme === "light"
              ? t("sidebar.theme.toDark")
              : theme === "dark"
                ? t("sidebar.theme.toLight")
                : t("sidebar.theme.followSystem")
          }
        >
          {theme === "light" ? (
            <Moon className="h-4 w-4" />
          ) : theme === "system" ? (
            <Monitor className="h-4 w-4" />
          ) : (
            <Sun className="h-4 w-4" />
          )}
        </button>
        <button
          onClick={toggleLanguage}
          className="xy-icon-btn"
          title={t("sidebar.languageToggle")}
        >
          <Languages className="h-4 w-4" />
        </button>
      </div>

      <div className="sr-only" aria-hidden>
        {SETTINGS_NAV_ITEMS.map((item) => (
          <NavLink key={item.hash} to={`/settings?tab=${item.hash}`}>
            {item.hash}
          </NavLink>
        ))}
      </div>
    </header>
  );
}

function Shell({
  theme,
  setTheme,
  toggleTheme,
}: {
  theme: string;
  setTheme: (t: string) => void;
  toggleTheme: () => void;
}) {
  // Every entry into the authenticated console shows the QQ group gate.
  const [qqOpen, setQqOpen] = useState(true);

  return (
    <div className="xy-app">
      <Deck theme={theme} toggleTheme={toggleTheme} />
      <main className="xy-canvas">
        <div className="xy-canvas-inner">
          <UpdateBanner />
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/accounts/chatgpt" element={<Accounts />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route
              path="/settings"
              element={<SettingsPage theme={theme} setTheme={setTheme} />}
            />
          </Routes>
        </div>
      </main>
      <QqGroupModal open={qqOpen} onClose={() => setQqOpen(false)} />
    </div>
  );
}

function AuthShell({
  code,
  brandSub,
  title,
  desc,
  children,
}: {
  code: string;
  brandSub: string;
  title: string;
  desc: string;
  children: ReactNode;
}) {
  const { t, toggleLanguage } = useI18n();
  return (
    <div className="xy-gate">
      <div className="xy-gate-frame">
        <header className="xy-gate-top">
          <div className="xy-mark">
            <div className="xy-mark-box">XY</div>
            <div>
              <div className="xy-mark-name">xyAUTO</div>
              <div className="xy-mark-tag">{t("brand.tag")}</div>
            </div>
          </div>
          <button
            type="button"
            onClick={toggleLanguage}
            className="xy-icon-btn"
            title={t("sidebar.languageToggle")}
          >
            <Languages className="h-4 w-4" />
          </button>
        </header>

        <div className="xy-gate-body">
          <div className="xy-gate-meta">
            <div className="xy-k">{code}</div>
            <p className="xy-gate-chip">{brandSub}</p>
          </div>
          <h1 className="xy-gate-title">{title}</h1>
          <p className="xy-gate-desc">{desc}</p>
          {children}
        </div>

        <footer className="xy-gate-foot">
          <span className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.12em] text-[var(--text-muted)]">
            ACCESS · LOCAL PANEL
          </span>
          <span className="xy-gate-lit" aria-hidden />
        </footer>
      </div>
    </div>
  );
}

function SetupScreen({ onDone }: { onDone: (token: string) => void }) {
  const { t } = useI18n();
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (pw.length < 6) {
      setError(t("setup.tooShort"));
      return;
    }
    if (pw !== pw2) {
      setError(t("setup.mismatch"));
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await fetch(API + "/auth/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw, password_confirm: pw2 }),
      });
      const data = await res.json();
      if (data.ok) {
        setAuthToken(data.token || "");
        onDone(data.token || "");
      } else {
        setError(data.error || t("login.requestFailed"));
      }
    } catch {
      setError(t("login.requestFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthShell
      code="00 · SETUP"
      brandSub={t("setup.brandSub")}
      title={t("setup.title")}
      desc={t("setup.desc")}
    >
      <form onSubmit={submit} className="xy-gate-form">
        <label className="xy-gate-field">
          <span className="xy-gate-label">{t("setup.password")}</span>
          <input
            type="password"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            placeholder={t("setup.password")}
            autoFocus
            autoComplete="new-password"
            className="control-surface control-surface-mono"
          />
        </label>
        <label className="xy-gate-field">
          <span className="xy-gate-label">{t("setup.passwordConfirm")}</span>
          <input
            type="password"
            value={pw2}
            onChange={(e) => setPw2(e.target.value)}
            placeholder={t("setup.passwordConfirm")}
            autoComplete="new-password"
            className="control-surface control-surface-mono"
          />
        </label>
        <p className="xy-gate-hint">{t("setup.hint")}</p>
        {error ? <div className="xy-gate-err" role="alert">{error}</div> : null}
        <button
          type="submit"
          disabled={loading || !pw || !pw2}
          className="xy-gate-submit"
        >
          {loading ? t("login.verifying") : t("setup.submit")}
        </button>
      </form>
    </AuthShell>
  );
}

function LoginScreen({ onLogin }: { onLogin: (token: string) => void }) {
  const { t } = useI18n();
  const [pw, setPw] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const res = await fetch(API + "/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      const data = await res.json();
      if (data.ok) {
        setAuthToken(data.token || "");
        onLogin(data.token || "");
      } else if (data.code === "setup_required") {
        window.location.reload();
      } else {
        setError(data.error || t("login.passwordError"));
      }
    } catch {
      setError(t("login.requestFailed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <AuthShell
      code="00 · AUTH"
      brandSub={t("login.brandSub")}
      title={t("login.prompt")}
      desc={t("login.desc")}
    >
      <form onSubmit={submit} className="xy-gate-form">
        <label className="xy-gate-field">
          <span className="xy-gate-label">{t("login.passwordPlaceholder")}</span>
          <input
            type="password"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            placeholder={t("login.passwordPlaceholder")}
            autoFocus
            autoComplete="current-password"
            className="control-surface control-surface-mono"
          />
        </label>
        {error ? <div className="xy-gate-err" role="alert">{error}</div> : null}
        <button
          type="submit"
          disabled={loading || !pw}
          className="xy-gate-submit"
        >
          {loading ? t("login.verifying") : t("login.enter")}
        </button>
      </form>
    </AuthShell>
  );
}

function AppContent() {
  const { t } = useI18n();
  const [theme, setTheme] = useState(
    () => localStorage.getItem("theme") || "dark",
  );
  const [authState, setAuthState] = useState<
    "loading" | "setup" | "locked" | "authed"
  >("loading");

  useEffect(() => {
    const applyTheme = () => {
      let effective = theme;
      if (theme === "system") {
        effective = window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark";
      }
      document.documentElement.classList.toggle("light", effective === "light");
    };
    applyTheme();
    localStorage.setItem("theme", theme);
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const handler = () => {
      if (theme === "system") applyTheme();
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme]);

  useEffect(() => {
    const token = getAuthToken();
    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    fetch(API + "/auth/check", { headers })
      .then((r) => r.json())
      .then((data) => {
        if (data.setup_required) {
          setAuthToken("");
          setAuthState("setup");
          return;
        }
        if (!data.required) {
          setAuthState("authed");
          return;
        }
        if (data.authenticated || token) {
          // Token present: try enter; middleware/api will 401-reload if invalid.
          setAuthState("authed");
          return;
        }
        setAuthState("locked");
      })
      .catch(() => {
        // Offline / first paint failure: if no token, show login; else try app.
        if (getAuthToken()) setAuthState("authed");
        else setAuthState("locked");
      });
  }, []);

  const toggleTheme = () =>
    setTheme((c) =>
      c === "dark" ? "light" : c === "light" ? "system" : "dark",
    );

  if (authState === "loading") {
    return (
      <div className="xy-gate">
        <div className="xy-gate-frame" style={{ width: "min(100%, 320px)" }}>
          <div className="xy-gate-top">
            <div className="xy-mark">
              <div className="xy-mark-box">XY</div>
              <div>
                <div className="xy-mark-name">xyAUTO</div>
                <div className="xy-mark-tag">{t("brand.tag")}</div>
              </div>
            </div>
          </div>
          <div className="xy-gate-body">
            <div className="xy-k">BOOT</div>
            <p className="xy-gate-desc mt-3 font-[family-name:var(--font-mono)] text-[11px] uppercase tracking-[0.14em]">
              {t("app.loading")}
            </p>
          </div>
        </div>
      </div>
    );
  }
  if (authState === "setup") {
    return <SetupScreen onDone={() => setAuthState("authed")} />;
  }
  if (authState === "locked") {
    return <LoginScreen onLogin={() => setAuthState("authed")} />;
  }

  return (
    <BrowserRouter>
      <Shell theme={theme} setTheme={setTheme} toggleTheme={toggleTheme} />
    </BrowserRouter>
  );
}

export default function App() {
  return (
    <I18nProvider>
      <LiveJobsProvider>
        <AppContent />
      </LiveJobsProvider>
    </I18nProvider>
  );
}

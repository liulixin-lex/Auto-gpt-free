import { useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Monitor,
  Moon,
  Plug,
  Save,
  ShieldCheck,
  Sun,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  getConfig,
  getConfigOptions,
  invalidateConfigCache,
} from "@/lib/app-data";
import type { ConfigOptionsResponse } from "@/lib/config-options";
import { localizeEventMessage, translateChoiceLabel } from "@/lib/i18n";
import { useI18n } from "@/lib/i18n-context";
import { apiFetch, cn } from "@/lib/utils";
import Settings from "@/pages/Settings";

const THEME_OPTIONS = [
  { value: "light", labelKey: "settings.theme.light", icon: Sun },
  { value: "dark", labelKey: "settings.theme.dark", icon: Moon },
  { value: "system", labelKey: "settings.theme.system", icon: Monitor },
] as const;

function errorMessage(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function ToggleRow({
  checked,
  onChange,
  title,
  desc,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  title: string;
  desc: string;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-2 border-[var(--border)] bg-[var(--bg-pane)] px-3 py-3">
      <div className="min-w-0">
        <div className="text-[13px] font-semibold text-[var(--text-primary)]">
          {title}
        </div>
        <p className="mt-0.5 text-[12px] leading-relaxed text-[var(--text-muted)]">
          {desc}
        </p>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cn(
          "relative mt-0.5 h-7 w-12 shrink-0 border-2 transition-colors",
          checked
            ? "border-[var(--accent)] bg-[var(--accent)]"
            : "border-[var(--border-hard)] bg-[var(--bg-input)]",
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 block h-4 w-4 border-2 border-black/20 bg-white transition-[left]",
            checked ? "left-6" : "left-0.5",
          )}
        />
      </button>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.1em] text-[var(--text-muted)]">
        {label}
      </span>
      {children}
    </label>
  );
}

function GeneralTab({
  theme,
  setTheme,
}: {
  theme: string;
  setTheme: (theme: string) => void;
}) {
  const { t, language, setLanguage } = useI18n();
  const [form, setForm] = useState<Record<string, string>>({});
  const [options, setOptions] = useState<ConfigOptionsResponse | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    Promise.all([
      getConfig().catch(() => ({})),
      getConfigOptions().catch(() => null),
    ]).then(([config, configOptions]) => {
      setForm(config);
      if (configOptions) setOptions(configOptions);
    });
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      await apiFetch("/config", {
        method: "PUT",
        body: JSON.stringify({ data: form }),
      });
      invalidateConfigCache();
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  };

  const executorOptions = options?.executor_options || [];
  const identityOptions = options?.identity_mode_options || [];

  return (
    <div className="space-y-3">
      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">{t("settings.appearance")}</h2>
        </div>
        <div className="xy-panel-b space-y-4">
          <div className="flex flex-wrap gap-2">
            {THEME_OPTIONS.map(({ value, labelKey, icon: Icon }) => (
              <button
                key={value}
                type="button"
                onClick={() => setTheme(value)}
                className={cn(
                  "inline-flex items-center gap-2 border px-3 py-2 text-[12px] font-semibold",
                  theme === value
                    ? "border-[var(--accent)] bg-[var(--accent)] text-[oklch(0.14_0_0)]"
                    : "border-[var(--border)] bg-[var(--bg-pane)] text-[var(--text-secondary)]",
                )}
              >
                <Icon className="h-3.5 w-3.5" />
                {t(labelKey)}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap gap-2 border-t border-[var(--border-soft)] pt-4">
            {([
              { value: "zh-CN", labelKey: "language.zh" },
              { value: "en-US", labelKey: "language.en" },
            ] as const).map(({ value, labelKey }) => (
              <button
                key={value}
                type="button"
                onClick={() => setLanguage(value)}
                className={cn(
                  "border px-3 py-2 text-[12px] font-semibold",
                  language === value
                    ? "border-[var(--accent)] bg-[var(--accent)] text-[oklch(0.14_0_0)]"
                    : "border-[var(--border)] bg-[var(--bg-pane)] text-[var(--text-secondary)]",
                )}
              >
                {t(labelKey)}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">{t("settings.registrationDefaults")}</h2>
        </div>
        <div className="xy-panel-b space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.identity")}>
              <select
                value={
                  form.default_identity_provider ||
                  identityOptions[0]?.value ||
                  ""
                }
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    default_identity_provider: event.target.value,
                  }))
                }
                className="control-surface appearance-none"
              >
                {identityOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {translateChoiceLabel(option.value, option.label, language)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("settings.executor")}>
              <select
                value={form.default_executor || executorOptions[0]?.value || ""}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    default_executor: event.target.value,
                  }))
                }
                className="control-surface appearance-none"
              >
                {executorOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Button onClick={save} disabled={saving}>
            <Save className="mr-2 h-3.5 w-3.5" />
            {saved ? t("settings.saved") : saving ? t("settings.saving") : t("settings.save")}
          </Button>
        </div>
      </div>
    </div>
  );
}

function NetworkTab() {
  const { t, language } = useI18n();
  const [form, setForm] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");

  const load = () =>
    getConfig({ force: true })
      .then((config) => setForm(config || {}))
      .catch(() => setForm({}));

  useEffect(() => {
    load();
  }, []);

  const isOn = (key: string) =>
    ["1", "true", "yes", "on"].includes(
      String(form[key] || "")
        .trim()
        .toLowerCase(),
    );
  const setBool = (key: string, value: boolean) =>
    setForm((current) => ({
      ...current,
      [key]: value ? "true" : "false",
    }));
  const flash = (text: string) => {
    setMessage(text);
    window.setTimeout(() => setMessage(""), 3200);
  };

  const persist = async () => {
    await apiFetch("/config", {
      method: "PUT",
      body: JSON.stringify({ data: form }),
    });
    invalidateConfigCache();
  };

  const save = async () => {
    setBusy("save");
    try {
      await persist();
      flash(t("settings.networkSaved"));
    } catch (error) {
      flash(localizeEventMessage(errorMessage(error, t("settings.configFailed")), language));
    } finally {
      setBusy("");
    }
  };

  const ensureFlaresolverr = async () => {
    setBusy("ensure");
    try {
      const result = await apiFetch("/network/runtime/ensure-fs", {
        method: "POST",
      });
      await load();
      flash(
        result?.flaresolverr_probe?.ok
          ? t("settings.flareSolverrEnabled")
          : t("settings.flareSolverrUnreachable"),
      );
    } catch (error) {
      flash(localizeEventMessage(errorMessage(error, t("settings.configFailed")), language));
    } finally {
      setBusy("");
    }
  };

  const testRuntime = async () => {
    setBusy("test");
    try {
      await persist();
      const result = await apiFetch("/network/runtime/test", {
        method: "POST",
      });
      flash(
        result?.ok
          ? t("settings.requestOk", { status: result.status_code || 200 })
          : t("settings.requestFailed", {
              reason: localizeEventMessage(
                String(result?.error || result?.status_code || "blocked"),
                language,
              ),
            }),
      );
    } catch (error) {
      flash(localizeEventMessage(errorMessage(error, t("settings.testFailed")), language));
    } finally {
      setBusy("");
    }
  };

  const clearanceMode = form.proxy_runtime_clearance_mode || "none";

  return (
    <div className="space-y-3">
      {message ? (
        <div
          role="status"
          className="border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-3 py-2 text-[13px] text-[var(--text-accent)]"
        >
          {message}
        </div>
      ) : null}

      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">{t("settings.networkTitle")}</h2>
          <span className="xy-lamp xy-lamp-cyan">CF / FS</span>
        </div>
        <div className="xy-panel-b space-y-3">
          <ToggleRow
            checked={isOn("proxy_runtime_enabled")}
            onChange={(value) => setBool("proxy_runtime_enabled", value)}
            title={t("settings.runtimeTitle")}
            desc={t("settings.runtimeDesc")}
          />

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.clearanceMode")}>
              <select
                value={clearanceMode}
                onChange={(event) => {
                  const mode = event.target.value;
                  setForm((current) => ({
                    ...current,
                    proxy_runtime_clearance_mode: mode,
                    proxy_runtime_enabled:
                      mode === "none" ? current.proxy_runtime_enabled : "true",
                    proxy_runtime_flaresolverr_url:
                      mode === "flaresolverr" &&
                      !current.proxy_runtime_flaresolverr_url
                        ? "http://flaresolverr:8191"
                        : current.proxy_runtime_flaresolverr_url,
                  }));
                }}
                className="control-surface appearance-none"
              >
                <option value="none">{t("settings.clearanceDisabled")}</option>
                <option value="flaresolverr">FlareSolverr</option>
                <option value="manual">{t("settings.clearanceManual")}</option>
              </select>
            </Field>
            <Field label={t("settings.scope")}>
              <select
                value={form.proxy_runtime_scope || "upstream_only"}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    proxy_runtime_scope: event.target.value,
                  }))
                }
                className="control-surface appearance-none"
              >
                <option value="upstream_only">ChatGPT / OpenAI</option>
                <option value="all">{t("settings.scopeAll")}</option>
              </select>
            </Field>
          </div>

          {clearanceMode === "flaresolverr" ? (
            <Field label="FlareSolverr URL">
              <input
                value={
                  form.proxy_runtime_flaresolverr_url ||
                  "http://flaresolverr:8191"
                }
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    proxy_runtime_flaresolverr_url: event.target.value,
                  }))
                }
                className="control-surface control-surface-mono"
              />
            </Field>
          ) : null}

          {clearanceMode === "manual" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="cf_clearance Cookie">
                <input
                  value={form.proxy_runtime_clearance_cookie || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      proxy_runtime_clearance_cookie: event.target.value,
                    }))
                  }
                  className="control-surface control-surface-mono"
                />
              </Field>
              <Field label="User-Agent">
                <input
                  value={form.proxy_runtime_clearance_ua || ""}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      proxy_runtime_clearance_ua: event.target.value,
                    }))
                  }
                  className="control-surface control-surface-mono"
                />
              </Field>
            </div>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label={t("settings.proxyUrl")}>
              <input
                value={form.proxy_runtime_proxy_url || ""}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    proxy_runtime_proxy_url: event.target.value,
                  }))
                }
                placeholder={t("settings.proxyPlaceholder")}
                className="control-surface control-surface-mono"
              />
            </Field>
            <Field label={t("settings.timeoutSeconds")}>
              <input
                type="number"
                min="5"
                max="120"
                value={form.proxy_runtime_timeout_sec || "25"}
                onChange={(event) =>
                  setForm((current) => ({
                    ...current,
                    proxy_runtime_timeout_sec: event.target.value,
                  }))
                }
                className="control-surface control-surface-mono"
              />
            </Field>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button onClick={save} disabled={!!busy}>
              <Save className="mr-2 h-3.5 w-3.5" />
              {busy === "save" ? t("settings.saving") : t("settings.save")}
            </Button>
            {clearanceMode === "flaresolverr" ? (
              <Button
                variant="outline"
                onClick={ensureFlaresolverr}
                disabled={!!busy}
              >
                <ShieldCheck className="mr-2 h-3.5 w-3.5" />
                {busy === "ensure" ? t("settings.enabling") : t("settings.enableFlaresolverr")}
              </Button>
            ) : null}
            <Button variant="outline" onClick={testRuntime} disabled={!!busy}>
              <Plug className="mr-2 h-3.5 w-3.5" />
              {busy === "test" ? t("settings.testingAccess") : t("settings.testAccess")}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function SettingsPage({
  theme,
  setTheme,
}: {
  theme: string;
  setTheme: (theme: string) => void;
}) {
  const { t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") || "general";
  const tab = ["general", "mailbox", "captcha", "network"].includes(
    requestedTab,
  )
    ? requestedTab
    : "general";
  const sections = [
    { hash: "general", label: t("settings.sectionGeneral"), code: "BASE" },
    { hash: "mailbox", label: t("settings.sectionMailbox"), code: "MAIL" },
    { hash: "captcha", label: t("settings.sectionCaptcha"), code: "CAPT" },
    { hash: "network", label: t("settings.sectionNetwork"), code: "NET" },
  ];

  return (
    <div className="xy-page">
      <div className="xy-strip">
        <div>
          <div className="xy-k">{t("settings.pageTitle")}</div>
          <h1 className="xy-h1">{t("settings.pageTitle")}</h1>
          <p className="xy-sub">{t("settings.pageSubtitle")}</p>
        </div>
      </div>

      <div className="xy-seg">
        {sections.map((section) => (
          <button
            key={section.hash}
            type="button"
            className={cn(tab === section.hash && "is-on")}
            onClick={() => setSearchParams({ tab: section.hash })}
          >
            <span className="mr-1.5 font-[family-name:var(--font-mono)] text-[10px] opacity-60">
              {section.code}
            </span>
            {section.label}
          </button>
        ))}
      </div>

      {tab === "general" ? (
        <GeneralTab theme={theme} setTheme={setTheme} />
      ) : null}
      {tab === "network" ? <NetworkTab /> : null}
      {tab === "mailbox" || tab === "captcha" ? (
        <div className="xy-panel">
          <div className="xy-panel-h">
            <h2 className="xy-panel-t">
              {tab === "mailbox"
                ? t("settings.title.mailbox")
                : t("settings.title.captcha")}
            </h2>
            <span className="xy-lamp xy-lamp-accent">
              {tab === "mailbox" ? "MAIL" : "CAPT"}
            </span>
          </div>
          <div className="xy-panel-b">
            <Settings providerType={tab as "mailbox" | "captcha"} />
          </div>
        </div>
      ) : null}
    </div>
  );
}

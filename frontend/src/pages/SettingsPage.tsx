import { useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Sun,
  Moon,
  Monitor,
  Save,
  Plug,
  Play,
  Radar,
  RefreshCw,
} from "lucide-react";
import { cn, apiFetch } from "@/lib/utils";
import {
  getConfig,
  getConfigOptions,
  invalidateConfigCache,
} from "@/lib/app-data";
import type { ConfigOptionsResponse } from "@/lib/config-options";
import { LANGUAGE_OPTIONS } from "@/lib/i18n";
import { useI18n } from "@/lib/i18n-context";
import { Button } from "@/components/ui/button";
import Settings from "@/pages/Settings";

const THEME_OPTIONS = [
  { value: "light", labelKey: "settings.theme.light", icon: Sun },
  { value: "dark", labelKey: "settings.theme.dark", icon: Moon },
  { value: "system", labelKey: "settings.theme.system", icon: Monitor },
] as const;

const SYNC_TARGET_OPTIONS = [
  { value: "none", label: "关闭同步" },
  { value: "cpa", label: "CLIProxyAPI" },
  { value: "sub2api", label: "Sub2API" },
  { value: "both", label: "两者都同步" },
] as const;

function ToggleRow({
  checked,
  onChange,
  title,
  desc,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
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

function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
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
  setTheme: (t: string) => void;
}) {
  const { t, language, setLanguage } = useI18n();
  const [form, setForm] = useState<Record<string, string>>({});
  const [configOptions, setConfigOptions] =
    useState<ConfigOptionsResponse | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    Promise.all([
      getConfig().catch(() => ({})),
      getConfigOptions().catch(() => null),
    ]).then(([cfg, opts]) => {
      setForm(cfg);
      if (opts) setConfigOptions(opts);
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
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  };

  const executorOptions = configOptions?.executor_options || [];
  const identityOptions = configOptions?.identity_mode_options || [];

  return (
    <div className="space-y-3">
      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">外观</h2>
        </div>
        <div className="xy-panel-b">
          <p className="mb-3 text-[13px] text-[var(--text-muted)]">
            主题：浅色 / 深色 / 跟随系统。
          </p>
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
        </div>
      </div>

      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">语言</h2>
        </div>
        <div className="xy-panel-b">
          <div className="flex flex-wrap gap-2">
            {LANGUAGE_OPTIONS.map(({ value, label }) => (
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
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">默认参数</h2>
        </div>
        <div className="xy-panel-b space-y-3">
          <p className="text-[13px] text-[var(--text-muted)]">
            注册时如果没有单独指定，会使用这里的默认值。
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="identity">
              <select
                value={
                  form.default_identity_provider ||
                  identityOptions[0]?.value ||
                  ""
                }
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    default_identity_provider: e.target.value,
                  }))
                }
                className="control-surface appearance-none"
              >
                {identityOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="executor">
              <select
                value={form.default_executor || executorOptions[0]?.value || ""}
                onChange={(e) =>
                  setForm((f) => ({ ...f, default_executor: e.target.value }))
                }
                className="control-surface appearance-none"
              >
                {executorOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Button onClick={save} disabled={saving}>
            <Save className="mr-2 h-3.5 w-3.5" />
            {saved ? "已保存" : saving ? "写入中…" : "写入基础设置"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SyncTab() {
  const [form, setForm] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<Record<string, any> | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState("");

  const load = async () => {
    const [cfg, st] = await Promise.all([
      getConfig({ force: true }).catch(() => ({})),
      apiFetch("/auto-ops/status").catch(() => null),
    ]);
    setForm(cfg || {});
    if (st) setStatus(st);
  };

  useEffect(() => {
    load();
  }, []);

  const setBool = (key: string, v: boolean) =>
    setForm((f) => ({ ...f, [key]: v ? "true" : "false" }));
  const isOn = (key: string) =>
    ["1", "true", "yes", "on"].includes(
      String(form[key] || "").trim().toLowerCase(),
    );

  const flash = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(""), 3200);
  };

  const save = async () => {
    setSaving(true);
    try {
      await apiFetch("/config", {
        method: "PUT",
        body: JSON.stringify({ data: form }),
      });
      invalidateConfigCache();
      setSaved(true);
      flash("同步与运维配置已保存");
      setTimeout(() => setSaved(false), 2000);
      await load();
    } catch (e: any) {
      flash(e?.message || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const testRemote = async (target: "cpa" | "sub2api") => {
    setBusy(`test-${target}`);
    try {
      const res = await apiFetch("/auto-ops/test-remote", {
        method: "POST",
        body: JSON.stringify({
          target,
          cpa_api_url: form.cpa_api_url || "",
          cpa_api_key: form.cpa_api_key || "",
          sub2api_base_url: form.sub2api_base_url || "",
          sub2api_token: form.sub2api_token || "",
          sub2api_email: form.sub2api_email || "",
          sub2api_password: form.sub2api_password || "",
        }),
      });
      flash(res?.message || (res?.ok ? "连接成功" : "连接失败"));
    } catch (e: any) {
      flash(e?.message || "连接测试失败");
    } finally {
      setBusy("");
    }
  };

  const runCycle = async () => {
    setBusy("cycle");
    try {
      await apiFetch("/auto-ops/run-cycle", { method: "POST" });
      flash("已触发一轮自动运维");
      await load();
    } catch (e: any) {
      flash(e?.message || "触发失败");
    } finally {
      setBusy("");
    }
  };

  const probeNow = async () => {
    setBusy("probe");
    try {
      const res = await apiFetch("/auto-ops/probe-now", { method: "POST" });
      flash(
        `探测完成：有效 ${res?.valid ?? 0} / 失效 ${res?.invalid ?? 0} / 异常 ${res?.error ?? 0}`,
      );
      await load();
    } catch (e: any) {
      flash(e?.message || "探测失败");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="space-y-3">
      {toast ? (
        <div className="border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-3 py-2 text-[13px] text-[var(--text-accent)]">
          {toast}
        </div>
      ) : null}

      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">远程同步目标</h2>
          <span className="xy-lamp xy-lamp-accent">SYNC</span>
        </div>
        <div className="xy-panel-b space-y-3">
          <p className="text-[13px] text-[var(--text-muted)]">
            仅上传本地校验通过的可用凭证。CLIProxyAPI：Codex OAuth
            （type=codex）。Sub2API：优先 Agent Identity；若 OpenAI 返回
            agent_registry_not_enabled（常见 free
            计划），自动回退为 Codex OAuth 导入，仍可出仓/上传。
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="同步目标">
              <select
                value={form.sync_target || "none"}
                onChange={(e) =>
                  setForm((f) => ({ ...f, sync_target: e.target.value }))
                }
                className="control-surface appearance-none"
              >
                {SYNC_TARGET_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
            <div className="flex items-end">
              <ToggleRow
                checked={isOn("auto_upload_enabled")}
                onChange={(v) => setBool("auto_upload_enabled", v)}
                title="注册后自动上传"
                desc="注册成功后推送：CPA=Codex OAuth；Sub2API=Agent Identity 或 OAuth 回退。"
              />
            </div>
          </div>
        </div>
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <div className="xy-panel">
          <div className="xy-panel-h">
            <h2 className="xy-panel-t">CLIProxyAPI</h2>
            <span className="xy-lamp">CPA</span>
          </div>
          <div className="xy-panel-b space-y-3">
            <p className="text-[12px] text-[var(--text-muted)]">
              导入格式：Codex OAuth token JSON（access/refresh/id_token +
              account_id）。官方 runtime 不识别 agentIdentity。
            </p>
            <Field label="管理端地址">
              <input
                value={form.cpa_api_url || ""}
                onChange={(e) =>
                  setForm((f) => ({ ...f, cpa_api_url: e.target.value }))
                }
                placeholder="http://127.0.0.1:8317"
                className="control-surface control-surface-mono"
              />
            </Field>
            <Field label="Management Key">
              <input
                type="password"
                value={form.cpa_api_key || ""}
                onChange={(e) =>
                  setForm((f) => ({ ...f, cpa_api_key: e.target.value }))
                }
                placeholder="Bearer management key"
                className="control-surface control-surface-mono"
                autoComplete="new-password"
              />
            </Field>
            <Button
              variant="outline"
              onClick={() => testRemote("cpa")}
              disabled={busy === "test-cpa"}
            >
              <Plug className="mr-2 h-3.5 w-3.5" />
              {busy === "test-cpa" ? "测试中…" : "测试连接"}
            </Button>
          </div>
        </div>

        <div className="xy-panel">
          <div className="xy-panel-h">
            <h2 className="xy-panel-t">Sub2API</h2>
            <span className="xy-lamp">S2A</span>
          </div>
          <div className="xy-panel-b space-y-3">
            <p className="text-[12px] text-[var(--text-muted)]">
              优先：sub2api-data + agentIdentity（免接码）。free
              号若未开通 Agent Registry，自动改为 Codex OAuth
              auth.json / tokens 导入。
            </p>
            <Field label="服务地址">
              <input
                value={form.sub2api_base_url || ""}
                onChange={(e) =>
                  setForm((f) => ({ ...f, sub2api_base_url: e.target.value }))
                }
                placeholder="http://127.0.0.1:8080"
                className="control-surface control-surface-mono"
              />
            </Field>
            <Field label="管理员 Token（可空，优先使用）">
              <input
                type="password"
                value={form.sub2api_token || ""}
                onChange={(e) =>
                  setForm((f) => ({ ...f, sub2api_token: e.target.value }))
                }
                placeholder="JWT access token"
                className="control-surface control-surface-mono"
                autoComplete="new-password"
              />
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="管理员邮箱">
                <input
                  value={form.sub2api_email || ""}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, sub2api_email: e.target.value }))
                  }
                  className="control-surface control-surface-mono"
                />
              </Field>
              <Field label="管理员密码">
                <input
                  type="password"
                  value={form.sub2api_password || ""}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      sub2api_password: e.target.value,
                    }))
                  }
                  className="control-surface control-surface-mono"
                  autoComplete="new-password"
                />
              </Field>
            </div>
            <Button
              variant="outline"
              onClick={() => testRemote("sub2api")}
              disabled={busy === "test-sub2api"}
            >
              <Plug className="mr-2 h-3.5 w-3.5" />
              {busy === "test-sub2api" ? "测试中…" : "测试连接"}
            </Button>
          </div>
        </div>
      </div>

      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">访问运行时 · 过盾</h2>
          <span className="xy-lamp xy-lamp-cyan">CF / FS</span>
        </div>
        <div className="xy-panel-b space-y-3">
          <p className="text-[12px] leading-relaxed text-[var(--text-muted)]">
            两件事分开：① 可选代理（换 IP）② Cloudflare 过盾（FlareSolverr 拿
            cf_clearance）。无代理也能过盾——FS 用本机出口解挑战。容器内地址填{" "}
            <code className="font-[family-name:var(--font-mono)] text-[var(--cyan)]">
              http://flaresolverr:8191
            </code>
            ，不要填 127.0.0.1。
          </p>

          <div className="grid gap-2 sm:grid-cols-3">
            <div className="border border-[var(--border-soft)] bg-[var(--bg-input)] px-3 py-2">
              <div className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.08em] text-[var(--text-muted)]">
                运行时
              </div>
              <div className="mt-0.5 text-[13px] font-semibold text-[var(--text-primary)]">
                {isOn("proxy_runtime_enabled") ? "已开启" : "关闭"}
              </div>
            </div>
            <div className="border border-[var(--border-soft)] bg-[var(--bg-input)] px-3 py-2">
              <div className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.08em] text-[var(--text-muted)]">
                出口路径
              </div>
              <div className="mt-0.5 text-[13px] font-semibold text-[var(--text-primary)]">
                {isOn("proxy_runtime_enabled") &&
                (form.proxy_runtime_proxy_url || "").trim()
                  ? "代理"
                  : "直连（本机 IP）"}
              </div>
            </div>
            <div className="border border-[var(--border-soft)] bg-[var(--bg-input)] px-3 py-2">
              <div className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.08em] text-[var(--text-muted)]">
                过盾
              </div>
              <div className="mt-0.5 text-[13px] font-semibold text-[var(--text-primary)]">
                {form.proxy_runtime_clearance_mode === "flaresolverr"
                  ? "FlareSolverr"
                  : form.proxy_runtime_clearance_mode === "manual"
                    ? "手动 Cookie"
                    : "未启用"}
              </div>
            </div>
          </div>

          <ToggleRow
            checked={isOn("proxy_runtime_enabled")}
            onChange={(v) => setBool("proxy_runtime_enabled", v)}
            title="启用访问运行时"
            desc="总开关。关闭 = 裸直连且不过盾。开启后才生效代理 / FlareSolverr。"
          />

          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="过盾模式（Cloudflare）">
              <select
                value={form.proxy_runtime_clearance_mode || "none"}
                onChange={(e) => {
                  const mode = e.target.value;
                  setForm((f) => ({
                    ...f,
                    proxy_runtime_clearance_mode: mode,
                    // Auto-fill compose FS URL when switching to flaresolverr
                    proxy_runtime_flaresolverr_url:
                      mode === "flaresolverr" &&
                      !(f.proxy_runtime_flaresolverr_url || "").trim()
                        ? "http://flaresolverr:8191"
                        : f.proxy_runtime_flaresolverr_url,
                    proxy_runtime_enabled:
                      mode !== "none" ? "true" : f.proxy_runtime_enabled,
                  }));
                }}
                className="control-surface appearance-none"
              >
                <option value="none">关闭（易被 CF 403）</option>
                <option value="flaresolverr">
                  FlareSolverr（推荐 · 无代理也可用）
                </option>
                <option value="manual">手动粘贴 cf_clearance</option>
              </select>
            </Field>
            <Field label="作用范围">
              <select
                value={form.proxy_runtime_scope || "upstream_only"}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    proxy_runtime_scope: e.target.value,
                  }))
                }
                className="control-surface appearance-none"
              >
                <option value="upstream_only">
                  仅 ChatGPT / OpenAI（推荐）
                </option>
                <option value="all">全部出站（含邮箱等）</option>
              </select>
            </Field>
          </div>

          {(form.proxy_runtime_clearance_mode || "none") === "flaresolverr" ? (
            <div className="space-y-2 border border-[var(--border-soft)] bg-[var(--bg-pane)] p-3">
              <div className="text-[12px] font-semibold text-[var(--text-primary)]">
                FlareSolverr
              </div>
              <p className="text-[11px] leading-relaxed text-[var(--text-muted)]">
                用无头浏览器过 CF 挑战，把 Cookie + UA 交给注册会话。与代理无关：不填代理 =
                用服务器本机 IP 过盾。
              </p>
              <Field label="服务地址（app 容器内）">
                <input
                  value={
                    form.proxy_runtime_flaresolverr_url ||
                    "http://flaresolverr:8191"
                  }
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      proxy_runtime_flaresolverr_url: e.target.value,
                    }))
                  }
                  placeholder="http://flaresolverr:8191"
                  className="control-surface control-surface-mono"
                />
              </Field>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  disabled={!!busy}
                  onClick={async () => {
                    setBusy("ensure-fs");
                    try {
                      const res = await apiFetch(
                        "/auto-ops/proxy-runtime/ensure-fs",
                        { method: "POST" },
                      );
                      setForm((f) => ({
                        ...f,
                        proxy_runtime_enabled: "true",
                        proxy_runtime_clearance_mode: "flaresolverr",
                        proxy_runtime_flaresolverr_url:
                          res?.flaresolverr_url ||
                          "http://flaresolverr:8191",
                        proxy_runtime_scope:
                          res?.scope || f.proxy_runtime_scope || "upstream_only",
                      }));
                      invalidateConfigCache();
                      const fsOk = res?.flaresolverr_probe?.ok;
                      flash(
                        fsOk
                          ? `已启用 FlareSolverr · ${res?.flaresolverr_url} · 服务正常`
                          : `已写入配置，但 FS 探测失败：${res?.flaresolverr_probe?.error || "unreachable"}`,
                      );
                    } catch (e: any) {
                      flash(e?.message || "一键配置失败");
                    } finally {
                      setBusy("");
                    }
                  }}
                >
                  {busy === "ensure-fs" ? "配置中…" : "一键启用 FlareSolverr"}
                </Button>
              </div>
            </div>
          ) : null}

          {(form.proxy_runtime_clearance_mode || "none") === "manual" ? (
            <Field label="手动 cf_clearance Cookie">
              <input
                value={form.proxy_runtime_clearance_cookie || ""}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    proxy_runtime_clearance_cookie: e.target.value,
                  }))
                }
                placeholder="cf_clearance=...; 须与下方 UA 一致"
                className="control-surface control-surface-mono"
              />
            </Field>
          ) : null}

          <div className="space-y-2 border border-dashed border-[var(--border)] p-3">
            <div className="text-[12px] font-semibold text-[var(--text-primary)]">
              可选代理（换 IP · 非必须）
            </div>
            <p className="text-[11px] leading-relaxed text-[var(--text-muted)]">
              留空 = 直连本机公网 IP。填 HTTP/SOCKS5h 后，ChatGPT 流量走代理；过盾 Cookie
              会绑定该出口。
            </p>
            <Field label="代理 URL">
              <input
                value={form.proxy_runtime_proxy_url || ""}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    proxy_runtime_proxy_url: e.target.value,
                  }))
                }
                placeholder="留空=直连 · 例 socks5h://user:pass@host:1080"
                className="control-surface control-surface-mono"
              />
            </Field>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={!!busy}
              onClick={async () => {
                setBusy("proxy-test");
                try {
                  // Persist current form first so test matches UI
                  await apiFetch("/config", {
                    method: "PUT",
                    body: JSON.stringify({ data: form }),
                  });
                  invalidateConfigCache();
                  const res = await apiFetch("/auto-ops/proxy-runtime/test", {
                    method: "POST",
                  });
                  if (res?.ok) {
                    flash(
                      `✓ 通 · 出口=${res.egress_path || res.proxy_url || "direct"} · HTTP ${res.status_code}` +
                        (res.clearance_applied ? " · 过盾已应用" : " · 未过盾") +
                        (res.hint ? ` · ${res.hint}` : ""),
                    );
                  } else {
                    flash(
                      `✗ 失败 · HTTP ${res?.status_code || "?"} · ${res?.error || "blocked"}` +
                        (res?.hint ? ` · ${res.hint}` : ""),
                    );
                  }
                } catch (e: any) {
                  flash(e?.message || "出口测试失败");
                } finally {
                  setBusy("");
                }
              }}
            >
              <Plug className="mr-2 h-3.5 w-3.5" />
              {busy === "proxy-test" ? "测试中…" : "测试访问 chatgpt.com"}
            </Button>
            <Button
              variant="outline"
              disabled={!!busy}
              onClick={async () => {
                setBusy("keepalive");
                try {
                  const res = await apiFetch("/auto-ops/token-keepalive", {
                    method: "POST",
                    body: JSON.stringify({ limit: 40, try_password: true }),
                  });
                  flash(
                    `续命完成 · 刷新 ${res?.refreshed ?? 0} · 重登 ${res?.relogin_ok ?? 0} · 失败 ${res?.failed ?? 0}`,
                  );
                } catch (e: any) {
                  flash(e?.message || "续命失败");
                } finally {
                  setBusy("");
                }
              }}
            >
              <RefreshCw className="mr-2 h-3.5 w-3.5" />
              {busy === "keepalive" ? "续命中…" : "立即 Token 续命"}
            </Button>
          </div>
        </div>
      </div>

      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">持续运维</h2>
          <span className="xy-lamp xy-lamp-accent">OPS</span>
        </div>
        <div className="xy-panel-b space-y-3">
          <div className="grid gap-2">
            <ToggleRow
              checked={isOn("auto_probe_enabled")}
              onChange={(v) => setBool("auto_probe_enabled", v)}
              title="自动持续探测"
              desc="按间隔巡检号池有效性，失效账号标记为已失效。"
            />
            <ToggleRow
              checked={isOn("auto_delete_remote_enabled")}
              onChange={(v) => setBool("auto_delete_remote_enabled", v)}
              title="自动删除失效号（本地+远程）"
              desc="探测失效后：先删远程凭据，再删本地号池记录，保持两边同步。"
            />
            <ToggleRow
              checked={
                form.auto_sync_delete_enabled === undefined ||
                form.auto_sync_delete_enabled === ""
                  ? true
                  : isOn("auto_sync_delete_enabled")
              }
              onChange={(v) => setBool("auto_sync_delete_enabled", v)}
              title="双向删除同步"
              desc="远端孤儿号（本地已无/已失效）自动删远程；远程被手动删掉则清除本地映射。"
            />
            <ToggleRow
              checked={isOn("auto_register_enabled")}
              onChange={(v) => setBool("auto_register_enabled", v)}
              title="自动启动注册"
              desc="允许运维周期在号池不足时自动创建注册任务。"
            />
            <ToggleRow
              checked={isOn("auto_replenish_enabled")}
              onChange={(v) => setBool("auto_replenish_enabled", v)}
              title="自动补号"
              desc="当有效号数低于目标库存时，按每次补号数量自动注册。"
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="运维周期（秒，最小 10）">
              <input
                type="number"
                min={10}
                value={form.auto_ops_interval_seconds || "30"}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    auto_ops_interval_seconds: e.target.value,
                    // keep minutes roughly in sync for older status displays
                    auto_probe_interval_minutes: String(
                      Math.max(1, Math.round(Number(e.target.value || 30) / 60)) || 1,
                    ),
                  }))
                }
                className="control-surface control-surface-mono"
              />
            </Field>
            <Field label="补号目标库存">
              <input
                type="number"
                min={0}
                value={form.auto_replenish_target || "10"}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    auto_replenish_target: e.target.value,
                  }))
                }
                className="control-surface control-surface-mono"
              />
            </Field>
            <Field label="每次补号数量">
              <input
                type="number"
                min={1}
                max={20}
                value={form.auto_register_count || "5"}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    auto_register_count: e.target.value,
                  }))
                }
                className="control-surface control-surface-mono"
              />
            </Field>
            <Field label="补号并发">
              <input
                type="number"
                min={1}
                max={20}
                value={form.auto_register_concurrency || "5"}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    auto_register_concurrency: e.target.value,
                  }))
                }
                className="control-surface control-surface-mono"
              />
            </Field>
            <Field label="自动注册执行器">
              <select
                value={form.auto_register_executor || form.default_executor || "protocol"}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    auto_register_executor: e.target.value,
                  }))
                }
                className="control-surface appearance-none"
              >
                <option value="protocol">协议</option>
                <option value="headless">无头浏览器</option>
                <option value="headed">有头浏览器</option>
              </select>
            </Field>
          </div>
          <p className="text-[12px] text-[var(--text-muted)]">
            Sub2API 持续消耗场景建议：周期 15–30 秒 · 补号数量/并发 5–10 · 开启探测+删号+补号+自动上传。
            注册成功即推送，无需等整批结束。
          </p>

          <div className="flex flex-wrap gap-2">
            <Button onClick={save} disabled={saving}>
              <Save className="mr-2 h-3.5 w-3.5" />
              {saved ? "已保存" : saving ? "写入中…" : "保存配置"}
            </Button>
            <Button
              variant="outline"
              onClick={probeNow}
              disabled={!!busy}
            >
              <Radar className="mr-2 h-3.5 w-3.5" />
              {busy === "probe" ? "探测中…" : "立即探测"}
            </Button>
            <Button
              variant="outline"
              onClick={runCycle}
              disabled={!!busy}
            >
              <Play className="mr-2 h-3.5 w-3.5" />
              {busy === "cycle" ? "执行中…" : "跑一轮运维"}
            </Button>
            <Button variant="outline" onClick={load} disabled={!!busy}>
              <RefreshCw className="mr-2 h-3.5 w-3.5" />
              刷新状态
            </Button>
          </div>
        </div>
      </div>

      <div className="xy-panel">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">运行状态</h2>
        </div>
        <div className="xy-panel-b grid gap-2 sm:grid-cols-2">
          <div className="xy-kv">
            <span>后台调度</span>
            <span>
              {status?.cycle_in_progress
                ? "本轮执行中"
                : status?.running
                  ? "运行中"
                  : "未启动"}
            </span>
          </div>
          <div className="xy-kv">
            <span>注册任务占用</span>
            <span>{status?.register_task_active ? "有进行中注册" : "空闲"}</span>
          </div>
          <div className="xy-kv">
            <span>最近周期</span>
            <span>{form.auto_ops_last_cycle_at || status?.last_cycle_at || "—"}</span>
          </div>
          <div className="xy-kv">
            <span>最近探测</span>
            <span>{form.auto_ops_last_probe_at || status?.last_probe_at || "—"}</span>
          </div>
          <div className="xy-kv">
            <span>探测结果</span>
            <span>
              {form.auto_ops_last_probe_result ||
                status?.last_probe_result ||
                "—"}
            </span>
          </div>
          <div className="xy-kv">
            <span>最近补号</span>
            <span>
              {form.auto_ops_last_replenish_at ||
                status?.last_replenish_at ||
                "—"}
            </span>
          </div>
          <div className="xy-kv sm:col-span-2">
            <span>说明</span>
            <span className="text-[var(--text-muted)]">
              详细过程日志请到「任务日志」查看 type=auto_ops 任务
            </span>
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
  setTheme: (t: string) => void;
}) {
  const { t } = useI18n();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab") || "general";
  const tab = ["general", "mailbox", "captcha", "sync"].includes(requestedTab)
    ? requestedTab
    : "general";

  const sections = [
    { hash: "general", label: "基础设置", code: "BASE" },
    { hash: "mailbox", label: "邮箱", code: "MAIL" },
    { hash: "captcha", label: "打码", code: "CAPT" },
    { hash: "sync", label: "同步", code: "SYNC" },
  ];

  return (
    <div className="xy-page">
      <div className="xy-strip">
        <div>
          <div className="xy-k">设置</div>
          <h1 className="xy-h1">设置</h1>
          <p className="xy-sub">
            管理界面、邮箱、打码、远程同步和自动运维。
          </p>
        </div>
      </div>

      <div className="xy-seg">
        {sections.map((s) => (
          <button
            key={s.hash}
            type="button"
            className={cn(tab === s.hash && "is-on")}
            onClick={() => setSearchParams({ tab: s.hash })}
          >
            <span className="mr-1.5 font-[family-name:var(--font-mono)] text-[10px] opacity-60">
              {s.code}
            </span>
            {s.label}
          </button>
        ))}
      </div>

      {tab === "general" && <GeneralTab theme={theme} setTheme={setTheme} />}
      {tab === "sync" && <SyncTab />}
      {(tab === "mailbox" || tab === "captcha") && (
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
      )}
    </div>
  );
}

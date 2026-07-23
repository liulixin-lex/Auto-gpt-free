import { useEffect, useMemo, useState, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { getConfigOptions } from '@/lib/app-data'
import type { ConfigOptionsResponse } from '@/lib/config-options'
import { getCaptchaStrategyLabel } from '@/lib/config-options'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import { buildExecutorOptions, buildRegistrationOptions } from '@/lib/registration'
import { Button } from '@/components/ui/button'
import { useLiveJobs } from '@/lib/live-jobs'
import { Copy, Download, X, Mail, Gauge, Cpu, Hash, Network } from 'lucide-react'
import {
  STATUS_VARIANT,
  getAccountOverview,
  getVerificationMailbox,
  getLifecycleStatus,
  getDisplayStatus,
  getPlanState,
  getValidityStatus,
  getPrimaryMetrics,
  getSecondaryMetrics,
  getDisplayWarnings,
  getDisplayBadges,
  getDisplaySections,
  getProviderAccounts,
  getCredentials,
  getCashierUrl,
  getPrimaryToken,
  ACCOUNT_EXPORT_FORMATS,
  formatResultValue,
} from '@/features/accounts/helpers'

const ACCOUNT_TOOL_BUTTON_CLASS = 'h-8 shrink-0 whitespace-nowrap bg-transparent'

// ── 注册弹框 ────────────────────────────────────────────────
export function RegisterModal({
  platformMeta,
  onClose,
  onDone,
}: {
  platformMeta: any
  onClose: () => void
  onDone: () => void
}) {
  const { t, language } = useI18n()
  const navigate = useNavigate()
  const { trackJob } = useLiveJobs()
  const [configOptions, setConfigOptions] = useState<ConfigOptionsResponse>({
    mailbox_providers: [],
    captcha_providers: [],
    mailbox_settings: [],
    captcha_settings: [],
    captcha_policy: {},
    executor_options: [],
    identity_mode_options: [],
  })
  const [configLoading, setConfigLoading] = useState(true)
  const [regCount, setRegCount] = useState(5)
  const [concurrency, setConcurrency] = useState(5)
  const [dynamicProxy, setDynamicProxy] = useState('')
  const [outlookPoolText, setOutlookPoolText] = useState('')
  const [protocolMailKey, setProtocolMailKey] = useState('')
  const [startError, setStartError] = useState('')
  const [selection, setSelection] = useState({
    identityProvider: 'mailbox',
    executorType: 'headless',
  })
  const [starting, setStarting] = useState(false)

  const supportedExecutors: string[] = platformMeta?.supported_executors || []
  const registrationOptions = buildRegistrationOptions(platformMeta, language)
  const executorOptions = buildExecutorOptions(
    supportedExecutors,
    platformMeta?.supported_executor_options || [],
    language,
  )
  const selectedRegistration = registrationOptions.find(option =>
    option.identityProvider === selection.identityProvider,
  )
  const selectedExecutor = executorOptions.find(option => option.value === selection.executorType)

  useEffect(() => {
    let active = true
    setConfigLoading(true)
    getConfigOptions()
      .then((options) => {
        if (!active) return
        if (options) setConfigOptions(options)
      })
      .catch(() => {
        if (!active) return
        setConfigOptions({
          mailbox_providers: [],
          captcha_providers: [],
          mailbox_settings: [],
          captcha_settings: [],
          captcha_policy: {},
          executor_options: [],
          identity_mode_options: [],
        })
      })
      .finally(() => {
        if (active) setConfigLoading(false)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (configLoading || registrationOptions.length === 0) return
    const defaultRegistration = registrationOptions[0]
    setSelection((current) => {
      const identityProvider = current.identityProvider || defaultRegistration.identityProvider
      const validExecutorOptions = buildExecutorOptions(
        supportedExecutors,
        platformMeta?.supported_executor_options || [],
        language,
      ).filter(option => !option.disabled)
      const preferredExecutor = supportedExecutors.includes('headless')
        ? 'headless'
        : supportedExecutors[0] || ''
      const executorType = validExecutorOptions.some(option => option.value === current.executorType)
        ? current.executorType
        : (validExecutorOptions.find(option => option.value === preferredExecutor)?.value || validExecutorOptions[0]?.value || '')
      if (
        current.identityProvider === identityProvider &&
        current.executorType === executorType
      ) {
        return current
      }
      return { identityProvider, executorType }
    })
  }, [configLoading, registrationOptions, supportedExecutors, language, platformMeta])

  useEffect(() => {
    if (!selection.identityProvider) return
    const validExecutorOptions = buildExecutorOptions(
      supportedExecutors,
      platformMeta?.supported_executor_options || [],
      language,
    ).filter(option => !option.disabled)
    if (!validExecutorOptions.some(option => option.value === selection.executorType)) {
      setSelection(current => {
        const nextExecutorType = validExecutorOptions[0]?.value || ''
        if (current.executorType === nextExecutorType) return current
        return { ...current, executorType: nextExecutorType }
      })
    }
  }, [selection.identityProvider, selection.executorType, supportedExecutors, language, platformMeta])

  const mailboxSettings = configOptions.mailbox_settings || []
  const defaultMailboxProvider =
    mailboxSettings.find((item) => item.is_default) || mailboxSettings[0] || null
  const protocolMailOptions = useMemo(() => {
    const fromSettings = mailboxSettings
      .filter((s) => s.enabled !== false)
      .map((s) => ({
        key: s.provider_key,
        label: s.display_name || s.provider_key,
      }))
    // Always offer Outlook pool even if not in settings list
    if (!fromSettings.some((o) => o.key === 'local_ms_pool')) {
      fromSettings.push({ key: 'local_ms_pool', label: 'Outlook 本地池' })
    }
    return fromSettings
  }, [mailboxSettings])

  useEffect(() => {
    if (protocolMailKey) return
    const preferred =
      defaultMailboxProvider?.provider_key ||
      protocolMailOptions.find((o) => o.key === 'cloud_mail')?.key ||
      protocolMailOptions[0]?.key ||
      ''
    if (preferred) setProtocolMailKey(preferred)
  }, [defaultMailboxProvider, protocolMailKey, protocolMailOptions])

  const effectiveProtocolMail =
    protocolMailKey || defaultMailboxProvider?.provider_key || 'cloud_mail'
  const verifyLabel =
    selection.executorType === 'protocol'
      ? t('accounts.protocolVerificationSummaryGeneric', {
          mail: effectiveProtocolMail,
        })
      : getCaptchaStrategyLabel(
          selection.executorType,
          configOptions.captcha_policy,
          configOptions.captcha_providers,
          language,
        )

  const start = async () => {
    setStarting(true)
    setStartError('')
    try {
      const extra: Record<string, any> = {
        identity_provider: selection.identityProvider,
      }
      if (selection.identityProvider === 'mailbox') {
        if (selection.executorType === 'protocol') {
          const mailKey = effectiveProtocolMail
          if (!mailKey) {
            throw new Error(t('accounts.missingDefaultMailbox'))
          }
          extra.mail_provider = mailKey
          if (mailKey === 'local_ms_pool') {
            if (!outlookPoolText.trim()) {
              throw new Error(t('accounts.outlookPoolRequired'))
            }
            extra.local_ms_pool_text = outlookPoolText.trim()
          }
        } else {
          if (!defaultMailboxProvider?.provider_key) {
            throw new Error(t('accounts.missingDefaultMailbox'))
          }
          extra.mail_provider = defaultMailboxProvider.provider_key
        }
      }
      const res = await apiFetch('/tasks/register', {
        method: 'POST',
        body: JSON.stringify({
          count: regCount,
          concurrency,
          executor_type: selection.executorType,
          captcha_solver: 'auto',
          proxy: dynamicProxy.trim() || null,
          extra,
        }),
      })
      trackJob(
        {
          taskId: res.task_id,
          title: `批量注册 ×${regCount} · ${platformMeta?.display_name || 'ChatGPT'}`,
          source: 'register',
        },
        { force: true },
      )
      onDone()
      onClose()
      navigate('/jobs')
    } catch (error: any) {
      setStartError(error?.message || String(error))
    } finally {
      setStarting(false)
    }
  }

  const dialog = (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel flex w-[min(920px,calc(100vw-24px))] max-w-none flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
        style={{ maxHeight: '90vh' }}
      >
        <div className="flex items-start justify-between gap-4 border-b border-[var(--border)] bg-[var(--bg-pane)] px-5 py-4">
          <div>
            <div className="xy-k">注册任务</div>
            <h2 className="mt-1 text-[18px] font-bold tracking-tight">
              填写注册参数
            </h2>
            <p className="mt-1 text-[12px] text-[var(--text-muted)]">
              提交后打开任务日志页查看进度。
            </p>
          </div>
          <button
            onClick={onClose}
            className="border border-transparent p-1.5 text-[var(--text-muted)] hover:border-[var(--border)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="grid min-h-0 flex-1 gap-0 overflow-hidden lg:grid-cols-[minmax(0,1.15fr)_280px]">
          <div className="space-y-4 overflow-y-auto px-5 py-4">
            {configLoading ? (
              <div className="text-[13px] text-[var(--text-muted)]">读取通道配置…</div>
            ) : (
              <>
                <section className="space-y-2">
                  <div className="flex items-center gap-2 text-[12px] font-bold uppercase tracking-[0.06em] text-[var(--text-secondary)]">
                    <Mail className="h-3.5 w-3.5 text-[var(--accent-strong)]" />
                    身份来源
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {registrationOptions.map(option => {
                      const active = selection.identityProvider === option.identityProvider
                      return (
                        <button
                          key={option.key}
                          type="button"
                          onClick={() => setSelection(current => ({
                            ...current,
                            identityProvider: option.identityProvider,
                          }))}
                          className={`border px-3 py-3 text-left transition-colors ${
                            active
                              ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                              : 'border-[var(--border)] bg-[var(--bg-input)] hover:border-[var(--accent-edge)]'
                          }`}
                        >
                          <div className="text-[13px] font-semibold">{option.label}</div>
                          <div className="mt-1 text-[11px] text-[var(--text-muted)]">{option.description}</div>
                        </button>
                      )
                    })}
                  </div>
                </section>

                <section className="space-y-2">
                  <div className="flex items-center gap-2 text-[12px] font-bold uppercase tracking-[0.06em] text-[var(--text-secondary)]">
                    <Cpu className="h-3.5 w-3.5 text-[var(--accent-strong)]" />
                    执行引擎
                  </div>
                  <div className="grid gap-2 sm:grid-cols-3">
                    {executorOptions.map(option => {
                      const active = selection.executorType === option.value
                      return (
                        <button
                          key={option.value}
                          type="button"
                          disabled={option.disabled}
                          onClick={() => !option.disabled && setSelection(current => ({ ...current, executorType: option.value }))}
                          className={`border px-3 py-3 text-left transition-colors ${
                            option.disabled
                              ? 'cursor-not-allowed border-[var(--border)] bg-[var(--bg-hover)] opacity-45'
                              : active
                                ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                                : 'border-[var(--border)] bg-[var(--bg-input)] hover:border-[var(--accent-edge)]'
                          }`}
                        >
                          <div className="text-[13px] font-semibold">{option.label}</div>
                          <div className="mt-1 text-[11px] text-[var(--text-muted)]">{option.description}</div>
                          {option.reason ? (
                            <div className="mt-1 text-[11px] text-[var(--warn)]">{option.reason}</div>
                          ) : null}
                        </button>
                      )
                    })}
                  </div>
                </section>

                {selection.executorType === 'protocol' ? (
                  <section className="space-y-2 border border-[var(--border)] bg-[var(--bg-pane)] p-3">
                    <label className="block space-y-1">
                      <span className="text-[12px] font-bold uppercase tracking-[0.06em] text-[var(--text-secondary)]">
                        {t('accounts.protocolMailLabel')}
                      </span>
                      <select
                        value={effectiveProtocolMail}
                        onChange={(e) => setProtocolMailKey(e.target.value)}
                        className="control-surface w-full appearance-none"
                      >
                        {protocolMailOptions.map((opt) => (
                          <option key={opt.key} value={opt.key}>
                            {opt.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <p className="text-[11px] text-[var(--text-muted)]">
                      {t('accounts.protocolMailHint')}
                    </p>
                    {effectiveProtocolMail === 'local_ms_pool' ? (
                      <>
                        <label className="block text-[12px] font-bold uppercase tracking-[0.06em] text-[var(--text-secondary)]">
                          {t('accounts.outlookPoolLabel')}
                        </label>
                        <p className="text-[11px] text-[var(--text-muted)]">
                          {t('accounts.outlookPoolHint')}
                        </p>
                        <textarea
                          value={outlookPoolText}
                          onChange={(event) => setOutlookPoolText(event.target.value)}
                          rows={6}
                          spellCheck={false}
                          placeholder={t('accounts.outlookPoolPlaceholder')}
                          className="control-surface w-full resize-y font-mono text-[12px]"
                        />
                      </>
                    ) : (
                      <div className="border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-3 py-2 text-[12px] text-[var(--text-secondary)]">
                        {t('accounts.protocolMailUsesSettings', {
                          mail: effectiveProtocolMail,
                        })}
                      </div>
                    )}
                  </section>
                ) : null}

                <section className="grid gap-3 sm:grid-cols-2">
                  <label className="block space-y-1">
                    <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-[0.08em] text-[var(--text-muted)]">
                      <Hash className="h-3 w-3" /> 数量
                    </span>
                    <input
                      type="number"
                      min={1}
                      max={99}
                      value={regCount}
                      onChange={e => setRegCount(Number(e.target.value))}
                      className="control-surface text-center font-mono"
                    />
                  </label>
                  <label className="block space-y-1">
                    <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-[0.08em] text-[var(--text-muted)]">
                      <Gauge className="h-3 w-3" /> 并发
                    </span>
                    <input
                      type="number"
                      min={1}
                      max={20}
                      value={concurrency}
                      onChange={e =>
                        setConcurrency(
                          Math.min(20, Math.max(1, Number(e.target.value) || 1)),
                        )
                      }
                      className="control-surface text-center font-mono"
                    />
                  </label>
                </section>

                <section className="space-y-1">
                  <span className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-[0.08em] text-[var(--text-muted)]">
                    <Network className="h-3 w-3" /> 代理（可选）
                  </span>
                  <input
                    type="text"
                    value={dynamicProxy}
                    onChange={(e) => setDynamicProxy(e.target.value)}
                    placeholder="http://user:pass@host:port"
                    spellCheck={false}
                    className="control-surface w-full font-mono text-[12px]"
                  />
                  <p className="text-[11px] text-[var(--text-muted)]">
                    有值则本批走该代理；空则直连。
                  </p>
                </section>

                {startError ? (
                  <div className="border border-[var(--danger)] bg-[var(--danger-soft)] px-3 py-2 text-[12px] text-[var(--danger)]">
                    {startError}
                  </div>
                ) : null}
              </>
            )}
          </div>

          <aside className="border-t border-[var(--border)] bg-[var(--bg-pane)] px-4 py-4 lg:border-l lg:border-t-0">
            <div className="xy-k">任务参数</div>
            <div className="mt-3 space-y-2">
              <div className="xy-kv">
                <span>target</span>
                <span>{platformMeta?.display_name || 'ChatGPT'}</span>
              </div>
              <div className="xy-kv">
                <span>identity</span>
                <span className="max-w-[120px] truncate text-right">{selectedRegistration?.label || '—'}</span>
              </div>
              <div className="xy-kv">
                <span>engine</span>
                <span className="max-w-[120px] truncate text-right">{selectedExecutor?.label || '—'}</span>
              </div>
              <div className="xy-kv">
                <span>verify</span>
                <span className="max-w-[120px] truncate text-right">{verifyLabel || '—'}</span>
              </div>
              <div className="xy-kv">
                <span>batch</span>
                <span>{regCount} × c{concurrency}</span>
              </div>
              <div className="xy-kv">
                <span>proxy</span>
                <span>{dynamicProxy.trim() ? 'on' : 'direct'}</span>
              </div>
            </div>
            <p className="mt-4 text-[11px] leading-relaxed text-[var(--text-muted)]">
              提交后跳转到「任务日志」查看进度。
            </p>
            <Button
              onClick={start}
              disabled={
                starting ||
                configLoading ||
                !selection.identityProvider ||
                !selection.executorType ||
                (selection.executorType === 'protocol' &&
                  effectiveProtocolMail === 'local_ms_pool' &&
                  !outlookPoolText.trim()) ||
                (selection.executorType === 'protocol' && !effectiveProtocolMail)
              }
              className="mt-4 w-full"
            >
              {starting ? '提交中…' : '提交并打开日志'}
            </Button>
            <Button variant="outline" onClick={onClose} className="mt-2 w-full" disabled={starting}>
              取消
            </Button>
          </aside>
        </div>
      </div>
    </div>
  )

  return typeof document !== 'undefined' ? createPortal(dialog, document.body) : dialog
}

export function ResultStat({ label, value }: { label: string; value: any }) {
  return (
    <div className="border-2 border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2">
      <div className="text-[11px] uppercase tracking-[0.16em] text-[var(--text-muted)]">{label}</div>
      <div className="mt-1 text-sm font-medium text-[var(--text-primary)] break-all">{formatResultValue(value)}</div>
    </div>
  )
}

function metricToneClass(tone?: string) {
  if (tone === 'good') {
    return 'border-[var(--ok)] bg-[var(--ok-soft)] text-[var(--ok)]'
  }
  if (tone === 'warning') {
    return 'border-[var(--warn)] bg-[var(--warn-soft)] text-[var(--warn)]'
  }
  if (tone === 'danger') {
    return 'border-[var(--danger)] bg-[var(--danger-soft)] text-[var(--danger)]'
  }
  return 'border-[var(--border)] bg-[var(--bg-hover)] text-[var(--text-primary)]'
}

function metricAccentClass(tone?: string) {
  if (tone === 'good') return 'from-emerald-400/70 to-cyan-300/50'
  if (tone === 'warning') return 'from-amber-300/80 to-orange-300/50'
  if (tone === 'danger') return 'from-red-400/80 to-rose-300/50'
  return 'from-[var(--accent)]/80 to-[var(--accent-strong)]/45'
}

export function DisplayMetricCard({ metric, compact = false }: { metric: any; compact?: boolean }) {
  return (
    <div className={`group relative overflow-hidden  border px-3.5 py-3 ${metricToneClass(metric?.tone)}`}>
      <div className={`pointer-events-none absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${metricAccentClass(metric?.tone)}`} />
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-[0.18em] opacity-65">{metric?.label || '-'}</div>
          {metric?.sub ? <div className="mt-1 truncate text-[11px] opacity-65">{metric.sub}</div> : null}
        </div>
        <div className={`${compact ? 'text-sm' : 'text-lg'} shrink-0 font-semibold tracking-[-0.03em]`}>{formatResultValue(metric?.value)}</div>
      </div>
      {typeof metric?.percent === 'number' ? (
        <div className="relative mt-3 h-1.5 overflow-hidden  bg-black/25">
          <div className={`h-full  bg-gradient-to-r ${metricAccentClass(metric?.tone)}`} style={{ width: `${Math.max(0, Math.min(100, metric.percent))}%` }} />
        </div>
      ) : null}
    </div>
  )
}

export function DisplayWarnings({ warnings }: { warnings: any[] }) {
  const { language } = useI18n()
  if (!warnings.length) return null
  const isEn = language === 'en-US'
  return (
    <div className="space-y-2">
      {warnings.map((item: any, index: number) => {
        const tone = item?.tone || 'warning'
        const lamp =
          tone === 'danger'
            ? 'xy-lamp-danger'
            : tone === 'good'
              ? 'xy-lamp-ok'
              : 'xy-lamp-warn'
        const badge =
          tone === 'danger'
            ? isEn
              ? 'Invalid'
              : '失效'
            : tone === 'good'
              ? isEn
                ? 'OK'
                : '正常'
              : isEn
                ? 'Notice'
                : '注意'
        let message = item?.message || '—'
        if (isEn && message === '账号当前检测为失效') {
          message = 'This account is currently marked invalid'
        } else if (isEn && message === '尚未完成有效性检测') {
          message = 'Validity check has not been completed yet'
        }
        return (
          <div
            key={`${item?.key || 'warning'}-${index}`}
            className={`border-2 px-3 py-3 text-[13px] font-semibold leading-snug ${metricToneClass(tone)}`}
          >
            <div className="mb-1.5 flex items-center gap-2">
              <span className={cn('xy-lamp', lamp)}>{badge}</span>
            </div>
            <div className="text-[var(--text-primary)]">{message}</div>
          </div>
        )
      })}
    </div>
  )
}

export function DisplaySections({ sections }: { sections: any[] }) {
  if (!sections.length) return null
  return (
    <div className="space-y-3">
      {sections.map((section: any) => (
        <div key={section?.key || section?.title} className="border-2 border-[var(--border)] bg-[var(--bg-hover)] p-3">
          <div className="text-xs font-semibold text-[var(--text-primary)]">{section?.title || '明细'}</div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {(Array.isArray(section?.items) ? section.items : []).map((item: any, index: number) => (
              <div key={`${item?.title || 'item'}-${index}`} className="border-2 border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">{item?.title || '-'}</div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  {(Array.isArray(item?.metrics) ? item.metrics : []).map((metric: any) => (
                    <div key={metric?.key || metric?.label}>
                      <span className="text-[var(--text-muted)]">{metric?.label || '-'}: </span>
                      <span>{formatResultValue(metric?.value)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

export function ActionResultHighlights({ payload }: { payload: any }) {
  if (!payload || typeof payload !== 'object') return null

  const stats: Array<{ label: string; value: any }> = []
  if ('valid' in payload) stats.push({ label: '账号有效', value: payload.valid })
  if (payload.membership_type) stats.push({ label: '套餐', value: payload.membership_type })
  if (payload.plan) stats.push({ label: '套餐', value: payload.plan })
  if (payload.plan_id) stats.push({ label: 'Plan ID', value: payload.plan_id })
  if (typeof payload.has_valid_payment_method === 'boolean') stats.push({ label: '已绑卡', value: payload.has_valid_payment_method })
  if ('trial_eligible' in payload) stats.push({ label: '可试用', value: payload.trial_eligible })
  if (payload.trial_length_days) stats.push({ label: '试用天数', value: payload.trial_length_days })
  if (payload.remaining_credits) stats.push({ label: '剩余额度', value: payload.remaining_credits })
  if (payload.usage_total) stats.push({ label: '已用额度', value: payload.usage_total })
  if (payload.plan_credits) stats.push({ label: '总额度', value: payload.plan_credits })
  if (stats.length === 0) return null
  return (
    <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {stats.map(item => <ResultStat key={item.label} label={item.label} value={item.value} />)}
    </div>
  )
}

export function ActionResultModal({
  title,
  payload,
  onClose,
}: {
  title: string
  payload: any
  onClose: () => void
}) {
  const content = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2)

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-lg"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{title}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">操作结果</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigator.clipboard.writeText(content)}>
              <Copy className="h-4 w-4 mr-1" />
              复制
            </Button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="px-6 py-4">
          <ActionResultHighlights payload={payload} />
          <pre className="bg-[var(--bg-hover)] border border-[var(--border)]  p-4 text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-all overflow-auto max-h-[65vh]">
            {content}
          </pre>
        </div>
      </div>
    </div>
  )
}

export function ActionParamsModal({
  action,
  initialValues,
  submitting,
  onClose,
  onSubmit,
}: {
  action: any
  initialValues: Record<string, string>
  submitting: boolean
  onClose: () => void
  onSubmit: (params: Record<string, string>) => void
}) {
  const [form, setForm] = useState<Record<string, string>>(initialValues)

  useEffect(() => {
    setForm(initialValues)
  }, [action?.id, initialValues])

  const params = Array.isArray(action?.params) ? action.params : []

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel dialog-panel-md"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border)]">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">{action?.label || '动作参数'}</h2>
            <p className="text-xs text-[var(--text-muted)] mt-0.5">填写执行该动作所需的参数</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="px-6 py-4 space-y-4">
          {params.map((param: any) => {
            const value = form[param.key] ?? ''
            if (Array.isArray(param.options) && param.options.length > 0) {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <select
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    className="w-full border-2 border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  >
                    {param.options.map((option: string) => (
                      <option key={option} value={option}>{option}</option>
                    ))}
                  </select>
                </label>
              )
            }
            if (param.type === 'textarea') {
              return (
                <label key={param.key} className="block">
                  <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                  <textarea
                    value={value}
                    onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                    rows={3}
                    className="w-full border-2 border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                  />
                </label>
              )
            }
            return (
              <label key={param.key} className="block">
                <div className="mb-1 text-xs text-[var(--text-muted)]">{param.label || param.key}</div>
                <input
                  type={param.type === 'number' ? 'number' : 'text'}
                  value={value}
                  onChange={e => setForm(current => ({ ...current, [param.key]: e.target.value }))}
                  className="w-full border-2 border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-sm outline-none focus:border-[var(--text-accent)]"
                />
              </label>
            )
          })}
        </div>
        <div className="px-6 py-4 border-t border-[var(--border)] flex gap-3">
          <Button onClick={() => onSubmit(form)} disabled={submitting} className="flex-1">
            {submitting ? '执行中...' : '执行'}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={submitting} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}
// ── 行操作（详情 / 检测 / 删除）────────────────────────────
export function ActionMenu({
  acc,
  onDetail,
  onDelete,
}: {
  acc: any
  onDetail: () => void
  onDelete: () => void
  onResult?: (title: string, payload: any) => void
  onChanged?: () => void
}) {
  const { trackJob } = useLiveJobs()
  const [running, setRunning] = useState(false)

  return (
    <div className="flex min-w-[120px] items-center justify-end gap-1.5 whitespace-nowrap">
      <button type="button" onClick={onDetail} className="table-action-btn">
        详情
      </button>
      <button
        type="button"
        className="table-action-btn"
        disabled={running}
        title="检测此账号"
        onClick={() => {
          setRunning(true)
          apiFetch(`/accounts/${acc.id}/check`, { method: 'POST' })
            .then((resp) => {
              setRunning(false)
              if (resp?.task_id) {
                trackJob(
                  {
                    taskId: resp.task_id,
                    title: `${acc.email} · 检测`,
                    source: 'batch',
                  },
                  { force: true },
                )
              }
            })
            .catch((e: any) => {
              setRunning(false)
              window.alert(e?.message || '检测请求失败')
            })
        }}
      >
        {running ? '…' : '检测'}
      </button>
      <button
        type="button"
        className="table-action-btn table-action-btn-danger"
        onClick={() => {
          if (!confirm(`确认删除 ${acc.email}？`)) return
          apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete)
        }}
      >
        删除
      </button>
    </div>
  )
}

// ── 账号详情弹框 ───────────────────────────────────────────
function normalizeLifecycle(value: string) {
  return value === 'invalid' ? 'invalid' : 'registered'
}

function lifecycleLabel(value: string) {
  return value === 'invalid' ? '已失效' : '已注册'
}

export function DetailModal({ acc, onClose, onSave }: { acc: any; onClose: () => void; onSave: () => void }) {
  const { trackJob } = useLiveJobs()
  const [form, setForm] = useState({
    lifecycle_status: normalizeLifecycle(getLifecycleStatus(acc)),
    primary_token: getPrimaryToken(acc),
    cashier_url: getCashierUrl(acc),
  })
  const [saving, setSaving] = useState(false)
  const [checking, setChecking] = useState(false)
  const [copied, setCopied] = useState('')
  const [tab, setTab] = useState<'overview' | 'secrets' | 'edit'>('overview')
  const overview = getAccountOverview(acc)
  const verificationMailbox = getVerificationMailbox(acc)
  const providerAccounts = getProviderAccounts(acc)
  const credentials = getCredentials(acc)
  const primaryMetrics = getPrimaryMetrics(acc)
  const secondaryMetrics = getSecondaryMetrics(acc)
  const warnings = getDisplayWarnings(acc)
  const displayBadges = getDisplayBadges(acc)
  const displaySections = getDisplaySections(acc)
  const platformCredentials = credentials.filter((item: any) => item.scope === 'platform')
  const displayStatus = getDisplayStatus(acc)
  const planState = getPlanState(acc)
  const validity = getValidityStatus(acc)

  const copyField = async (label: string, value: string) => {
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
      setCopied(label)
      window.setTimeout(() => setCopied(''), 1200)
    } catch {
      // ignore
    }
  }

  const save = async () => {
    setSaving(true)
    try {
      await apiFetch(`/accounts/${acc.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          ...form,
          lifecycle_status: normalizeLifecycle(form.lifecycle_status),
        }),
      })
      onSave()
    } finally {
      setSaving(false)
    }
  }

  const statusLamp =
    STATUS_VARIANT[displayStatus] === 'danger' || displayStatus === 'invalid'
      ? 'xy-lamp-danger'
      : STATUS_VARIANT[displayStatus] === 'success'
        ? 'xy-lamp-ok'
        : STATUS_VARIANT[displayStatus] === 'warning'
          ? 'xy-lamp-warn'
          : 'xy-lamp-accent'

  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        className="dialog-panel flex w-[min(880px,calc(100vw-20px))] max-w-none flex-col overflow-hidden"
        style={{ maxHeight: '92vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* top bar */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-b-2 border-[var(--border-hard)] bg-[var(--bg-input)] px-4 py-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className={cn('xy-lamp', statusLamp)}>{displayStatus}</span>
              <span className="xy-lamp xy-lamp-cyan">
                {lifecycleLabel(normalizeLifecycle(getLifecycleStatus(acc)))}
              </span>
              <span className="xy-lamp">
                {acc.plan_name || overview.plan_name || overview.plan || planState || '未知套餐'}
              </span>
            </div>
            <div className="mt-2 truncate font-[family-name:var(--font-mono)] text-[14px] font-semibold">
              {acc.email}
            </div>
          </div>
          <button
            onClick={onClose}
            className="border-2 border-[var(--border)] p-1.5 text-[var(--text-muted)] hover:border-[var(--accent-edge)] hover:text-[var(--text-primary)]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* body: left rail + right content */}
        <div className="grid min-h-0 flex-1 md:grid-cols-[200px_minmax(0,1fr)]">
          <aside className="border-b-2 border-[var(--border)] bg-[var(--bg-pane)] md:border-b-0 md:border-r-2">
            <div className="space-y-1 p-3">
              {(
                [
                  { id: 'overview' as const, label: '概览' },
                  { id: 'secrets' as const, label: '凭据' },
                  { id: 'edit' as const, label: '编辑' },
                ] as const
              ).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setTab(item.id)}
                  className={cn(
                    'w-full border-2 px-3 py-2 text-left text-[12px] font-semibold',
                    tab === item.id
                      ? 'border-[var(--accent)] bg-[var(--accent-soft)] text-[var(--text-primary)]'
                      : 'border-transparent bg-transparent text-[var(--text-muted)] hover:border-[var(--border)] hover:text-[var(--text-primary)]',
                  )}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <div className="space-y-2 border-t-2 border-[var(--border-soft)] p-3 text-[12px]">
              <div className="xy-kv">
                <span>有效性</span>
                <span>{validity || '—'}</span>
              </div>
              <div className="xy-kv">
                <span>密码</span>
                <span className="max-w-[120px] truncate break-all">{acc.password || '—'}</span>
              </div>
              {verificationMailbox?.email ? (
                <div className="break-all text-[11px] text-[var(--text-muted)]">
                  验证箱 {verificationMailbox.email}
                </div>
              ) : null}
              {copied ? (
                <div className="text-[11px] text-[var(--ok)]">已复制 {copied}</div>
              ) : null}
            </div>
          </aside>

          <div className="min-h-0 overflow-y-auto p-4">
            {tab === 'overview' && (
              <div className="space-y-4">
                <section className="border-2 border-[var(--border)] bg-[var(--bg-input)] p-3">
                  <div className="mb-2 text-[12px] font-semibold text-[var(--text-secondary)]">关键信息</div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {[
                      ['邮箱', acc.email],
                      ['套餐', acc.plan_name || overview.plan_name || overview.plan || planState || '—'],
                      ['生命周期', lifecycleLabel(normalizeLifecycle(getLifecycleStatus(acc)))],
                      ['有效性', validity || '—'],
                    ].map(([k, v]) => (
                      <div key={k as string} className="border border-[var(--border-soft)] bg-[var(--bg-pane)] px-2.5 py-2">
                        <div className="text-[10px] text-[var(--text-muted)]">{k}</div>
                        <div className="mt-0.5 break-all font-[family-name:var(--font-mono)] text-[12px]">{v as string}</div>
                      </div>
                    ))}
                  </div>
                </section>

                {(primaryMetrics.length > 0 || secondaryMetrics.length > 0) && (
                  <section>
                    <div className="mb-2 text-[12px] font-semibold text-[var(--text-secondary)]">指标</div>
                    <div className="flex flex-col gap-1.5">
                      {[...primaryMetrics, ...secondaryMetrics.slice(0, 6)].map((metric: any) => (
                        <div
                          key={metric.key || metric.label}
                          className="flex items-center justify-between gap-3 border-2 border-[var(--border)] bg-[var(--bg-pane)] px-3 py-2"
                        >
                          <div className="min-w-0 text-[12px] text-[var(--text-muted)]">
                            {metric?.label || '-'}
                            {metric?.sub ? ` · ${metric.sub}` : ''}
                          </div>
                          <div className="shrink-0 font-[family-name:var(--font-mono)] text-[13px] font-semibold">
                            {formatResultValue(metric?.value)}
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>
                )}

                <DisplayWarnings warnings={warnings} />
                <DisplaySections sections={displaySections} />

                {displayBadges.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {displayBadges.map((badge: any, index: number) => (
                      <span key={`${badge?.label || 'badge'}-${index}`} className="xy-lamp">
                        {badge?.label}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}

            {tab === 'secrets' && (
              <div className="space-y-3">
                {platformCredentials.length === 0 && providerAccounts.length === 0 ? (
                  <div className="empty-state-panel">暂无凭据</div>
                ) : null}

                {platformCredentials.map((item: any) => (
                  <div key={`${item.scope}-${item.key}`} className="border-2 border-[var(--border)] bg-[var(--bg-pane)] p-3">
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <span className="font-[family-name:var(--font-mono)] text-[11px] text-[var(--text-muted)]">
                        {item.key}
                      </span>
                      <button
                        type="button"
                        onClick={() => copyField(item.key, String(item.value || ''))}
                        className="table-action-btn"
                      >
                        复制
                      </button>
                    </div>
                    <div className="max-h-32 overflow-y-auto border border-[var(--border-soft)] bg-[var(--bg-input)] px-2 py-1.5 font-[family-name:var(--font-mono)] text-[11px] break-all text-[var(--text-secondary)]">
                      {item.value}
                    </div>
                  </div>
                ))}

                {providerAccounts.map((item: any, index: number) => (
                  <div
                    key={`${item.provider_name || 'provider'}-${item.login_identifier || index}`}
                    className="border-2 border-[var(--border)] bg-[var(--bg-pane)] p-3"
                  >
                    <div className="text-[12px] font-semibold">
                      {item.provider_name || item.provider_type || 'provider'}
                    </div>
                    <div className="mt-1 break-all font-[family-name:var(--font-mono)] text-[11px] text-[var(--text-muted)]">
                      {item.login_identifier || '—'}
                    </div>
                    {item.credentials && Object.keys(item.credentials).length > 0 && (
                      <div className="mt-2 space-y-2">
                        {Object.entries(item.credentials).map(([key, value]: [string, any]) => (
                          <div key={key}>
                            <div className="mb-1 flex items-center justify-between">
                              <span className="font-[family-name:var(--font-mono)] text-[10px] text-[var(--text-muted)]">
                                {key}
                              </span>
                              {value ? (
                                <button
                                  type="button"
                                  onClick={() => copyField(key, String(value))}
                                  className="table-action-btn"
                                >
                                  复制
                                </button>
                              ) : null}
                            </div>
                            <div className="max-h-24 overflow-y-auto border border-[var(--border-soft)] bg-[var(--bg-input)] px-2 py-1.5 font-[family-name:var(--font-mono)] text-[11px] break-all">
                              {String(value || '—')}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {tab === 'edit' && (
              <div className="space-y-4">
                <div>
                  <div className="mb-2 text-[12px] font-semibold text-[var(--text-secondary)]">状态</div>
                  <div className="flex gap-2">
                    {([
                      { value: 'registered', label: '已注册' },
                      { value: 'invalid', label: '已失效' },
                    ] as const).map((opt) => {
                      const on = form.lifecycle_status === opt.value
                      return (
                        <button
                          key={opt.value}
                          type="button"
                          onClick={() => setForm((f) => ({ ...f, lifecycle_status: opt.value }))}
                          className={cn(
                            'flex-1 border-2 px-3 py-3 text-[13px] font-semibold',
                            on
                              ? 'border-[var(--accent)] bg-[var(--accent-soft)]'
                              : 'border-[var(--border)] bg-[var(--bg-input)]',
                          )}
                        >
                          {opt.label}
                        </button>
                      )
                    })}
                  </div>
                </div>

                <label className="block space-y-1">
                  <span className="text-[12px] font-semibold text-[var(--text-secondary)]">主凭证</span>
                  <textarea
                    value={form.primary_token}
                    onChange={(e) => setForm((f) => ({ ...f, primary_token: e.target.value }))}
                    rows={4}
                    className="control-surface control-surface-mono resize-y"
                    spellCheck={false}
                  />
                </label>

                <label className="block space-y-1">
                  <span className="text-[12px] font-semibold text-[var(--text-secondary)]">试用链接</span>
                  <textarea
                    value={form.cashier_url}
                    onChange={(e) => setForm((f) => ({ ...f, cashier_url: e.target.value }))}
                    rows={2}
                    className="control-surface control-surface-mono resize-y"
                    spellCheck={false}
                  />
                </label>
              </div>
            )}
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap gap-2 border-t-2 border-[var(--border-hard)] bg-[var(--bg-pane)] px-4 py-3">
          <Button
            variant="outline"
            disabled={checking || saving}
            onClick={async () => {
              setChecking(true)
              try {
                const resp = await apiFetch(`/accounts/${acc.id}/check`, { method: 'POST' })
                if (resp?.task_id) {
                  trackJob(
                    {
                      taskId: resp.task_id,
                      title: `${acc.email} · 检测`,
                      source: 'batch',
                    },
                    { force: true },
                  )
                  onClose()
                }
              } catch (e: any) {
                window.alert(e?.message || '检测请求失败')
              } finally {
                setChecking(false)
              }
            }}
          >
            {checking ? '提交中…' : '检测'}
          </Button>
          <Button onClick={save} disabled={saving || checking} className="flex-1">
            {saving ? '保存中…' : '保存'}
          </Button>
          <Button variant="outline" onClick={onClose} className="flex-1" disabled={saving || checking}>
            关闭
          </Button>
        </div>
      </div>
    </div>
  )
}

function cn(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(' ')
}

// ── 导入弹框 ────────────────────────────────────────────────
export function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const submit = async () => {
    setLoading(true)
    try {
      const lines = text.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', { method: 'POST', body: JSON.stringify({ platform: 'chatgpt', lines }) })
      setResult(`导入成功 ${res.created} 个`); onDone()
    } catch (e: any) { setResult(`失败: ${e.message}`) } finally { setLoading(false) }
  }
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm p-6" onClick={e => e.stopPropagation()}>
        <h2 className="text-base font-semibold text-[var(--text-primary)] mb-2">批量导入</h2>
        <p className="text-xs text-[var(--text-muted)] mb-3">每行格式: <code className="bg-[var(--bg-hover)] px-1 rounded">email password [cashier_url]</code></p>
        <textarea value={text} onChange={e => setText(e.target.value)} rows={8}
          className="control-surface control-surface-mono resize-none mb-3" />
        {result && <p className="text-sm text-emerald-400 mb-3">{result}</p>}
        <div className="flex gap-2">
          <Button onClick={submit} disabled={loading} className="flex-1">{loading ? '导入中...' : '导入'}</Button>
          <Button variant="outline" onClick={onClose} className="flex-1">取消</Button>
        </div>
      </div>
    </div>
  )
}

export function ExportMenu({
  total,
  statusFilter,
  searchFilter,
  selectedIds,
  layout = 'menu',
}: {
  total: number
  statusFilter: string
  searchFilter: string
  selectedIds: number[]
  layout?: 'menu' | 'grid'
}) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const hasSelection = selectedIds.length > 0

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const doExport = async (format: string) => {
    setLoading(format)
    try {
      const { blob, filename } = await apiDownload(`/accounts/export/${format}`, {
        method: 'POST',
        body: JSON.stringify({
          platform: 'chatgpt',
          ids: hasSelection ? selectedIds : [],
          select_all: !hasSelection,
          status_filter: !hasSelection ? statusFilter || null : null,
          search_filter: !hasSelection ? searchFilter || null : null,
        }),
      })
      triggerBrowserDownload(blob, filename)
      setOpen(false)
    } catch (e: any) {
      window.alert(e?.message || '出仓失败')
    } finally {
      setLoading(null)
    }
  }

  if (layout === 'grid') {
    return (
      <div className="grid gap-2 sm:grid-cols-3">
        {ACCOUNT_EXPORT_FORMATS.map((option) => (
          <button
            key={option.key}
            type="button"
            disabled={total === 0 || !!loading}
            onClick={() => doExport(option.key)}
            className="border border-[var(--border)] bg-[var(--bg-input)] px-3 py-3 text-left transition-colors hover:border-[var(--accent-edge)] hover:bg-[var(--accent-soft)] disabled:opacity-45"
          >
            <div className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.08em] text-[var(--accent-strong)]">
              {loading === option.key ? 'writing…' : option.key}
            </div>
            <div className="mt-1 text-[13px] font-semibold text-[var(--text-primary)]">
              {option.label}
            </div>
            <div className="mt-1 text-[11px] text-[var(--text-muted)]">{option.hint}</div>
          </button>
        ))}
      </div>
    )
  }

  const menu = open ? (
    <div className="absolute right-0 top-full z-[80] mt-1 min-w-[260px] border-2 border-[var(--border-hard)] bg-[var(--bg-card)] py-1 shadow-[var(--shadow-hard)]">
      <div className="border-b border-[var(--border-soft)] px-3 py-1.5 font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.08em] text-[var(--text-muted)]">
        {hasSelection
          ? `scope · ${selectedIds.length} marked`
          : 'scope · current filter'}
      </div>
      {ACCOUNT_EXPORT_FORMATS.map((option) => (
        <button
          key={option.key}
          type="button"
          onClick={() => doExport(option.key)}
          className="w-full px-3 py-2 text-left hover:bg-[var(--bg-hover)]"
        >
          <div className="text-[12px] font-semibold text-[var(--text-primary)]">
            {option.label}
          </div>
          <div className="text-[11px] text-[var(--text-muted)]">{option.hint}</div>
        </button>
      ))}
    </div>
  ) : null

  return (
    <div className="relative z-[40]" ref={menuRef}>
      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen((v) => !v)}
        disabled={total === 0 || !!loading}
        className={ACCOUNT_TOOL_BUTTON_CLASS}
      >
        <Download className="mr-1 h-4 w-4 shrink-0" />
        {loading
          ? t('accounts.exporting')
          : hasSelection
            ? t('accounts.exportSelected', { count: selectedIds.length })
            : t('accounts.exportBay')}
      </Button>
      {menu}
    </div>
  )
}

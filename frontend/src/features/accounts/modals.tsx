import { useEffect, useMemo, useState, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { getConfigOptions } from '@/lib/app-data'
import type { ConfigOptionsResponse } from '@/lib/config-options'
import { getCaptchaStrategyLabel } from '@/lib/config-options'
import { apiDownload, apiFetch, triggerBrowserDownload } from '@/lib/utils'
import { useI18n } from '@/lib/i18n-context'
import { localizeEventMessage, translateAccountStatus } from '@/lib/i18n'
import { buildExecutorOptions, buildRegistrationOptions } from '@/lib/registration'
import { Button } from '@/components/ui/button'
import { useLiveJobs } from '@/lib/live-jobs'
import { Download, X, Mail, Gauge, Cpu, Hash, Network } from 'lucide-react'
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
  getAccountExportFormats,
  formatResultValue,
} from '@/features/accounts/helpers'

const ACCOUNT_TOOL_BUTTON_CLASS = 'h-8 shrink-0 whitespace-nowrap bg-transparent'

// Registration modal──────────────────
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
  const [concurrency, setConcurrency] = useState(1)
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
      fromSettings.push({ key: 'local_ms_pool', label: t('register.outlookPoolFallback') })
    }
    return fromSettings
  }, [mailboxSettings, t])

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
  const verifyLabel = localizeEventMessage(
    selection.executorType === 'protocol'
      ? t('accounts.protocolVerificationSummaryGeneric', {
          mail: effectiveProtocolMail,
        })
      : getCaptchaStrategyLabel(
          selection.executorType,
          configOptions.captcha_policy,
          configOptions.captcha_providers,
          language,
        ),
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
          title: `${t('register.taskTitle')} ×${regCount} · ${platformMeta?.display_name || 'ChatGPT'}`,
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
            <div className="xy-k">{t('register.taskTitle')}</div>
            <h2 className="mt-1 text-[18px] font-bold tracking-tight">
              {t('register.taskHeading')}
            </h2>
            <p className="mt-1 text-[12px] text-[var(--text-muted)]">
              {t('register.taskDescription')}
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
              <div className="text-[13px] text-[var(--text-muted)]">{t('register.loadingConfig')}</div>
            ) : (
              <>
                <section className="space-y-2">
                  <div className="flex items-center gap-2 text-[12px] font-bold uppercase tracking-[0.06em] text-[var(--text-secondary)]">
                    <Mail className="h-3.5 w-3.5 text-[var(--accent-strong)]" />
                    {t('register.identitySource')}
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
                    {t('register.executor')}
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
                      <Hash className="h-3 w-3" /> {t('register.quantity')}
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
                      <Gauge className="h-3 w-3" /> {t('register.concurrency')}
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
                    <Network className="h-3 w-3" /> {t('register.proxyLabel')}
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
                    {t('register.proxyHint')}
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
            <div className="xy-k">{t('register.taskParams')}</div>
            <div className="mt-3 space-y-2">
              <div className="xy-kv">
                <span>{t('register.target')}</span>
                <span>{platformMeta?.display_name || 'ChatGPT'}</span>
              </div>
              <div className="xy-kv">
                <span>{t('register.identity')}</span>
                <span className="max-w-[120px] truncate text-right">{selectedRegistration?.label || '—'}</span>
              </div>
              <div className="xy-kv">
                <span>{t('register.engine')}</span>
                <span className="max-w-[120px] truncate text-right">{selectedExecutor?.label || '—'}</span>
              </div>
              <div className="xy-kv">
                <span>{t('register.verify')}</span>
                <span className="max-w-[120px] truncate text-right">{verifyLabel || '—'}</span>
              </div>
              <div className="xy-kv">
                <span>{t('register.batch')}</span>
                <span>{regCount} × c{concurrency}</span>
              </div>
              <div className="xy-kv">
                <span>{t('register.proxyState')}</span>
                <span>{dynamicProxy.trim() ? t('register.proxyOn') : t('register.proxyDirect')}</span>
              </div>
            </div>
            <p className="mt-4 text-[11px] leading-relaxed text-[var(--text-muted)]">
              {t('register.submitHint')}
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
              {starting ? t('register.submitting') : t('register.submitAndOpenLogs')}
            </Button>
            <Button variant="outline" onClick={onClose} className="mt-2 w-full" disabled={starting}>
              {t('common.cancel')}
            </Button>
          </aside>
        </div>
      </div>
    </div>
  )

  return typeof document !== 'undefined' ? createPortal(dialog, document.body) : dialog
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
  const { language } = useI18n()
  return (
    <div className={`group relative overflow-hidden  border px-3.5 py-3 ${metricToneClass(metric?.tone)}`}>
      <div className={`pointer-events-none absolute inset-y-0 left-0 w-1 bg-gradient-to-b ${metricAccentClass(metric?.tone)}`} />
      <div className="relative flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-[0.18em] opacity-65">
            {metric?.label ? localizeEventMessage(metric.label, language) : '-'}
          </div>
          {metric?.sub ? (
            <div className="mt-1 truncate text-[11px] opacity-65">
              {localizeEventMessage(metric.sub, language)}
            </div>
          ) : null}
        </div>
        <div className={`${compact ? 'text-sm' : 'text-lg'} shrink-0 font-semibold tracking-[-0.03em]`}>{formatResultValue(metric?.value, language)}</div>
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
  const { t, language } = useI18n()
  if (!warnings.length) return null
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
            ? t('accounts.warningInvalid')
            : tone === 'good'
              ? t('accounts.warningOk')
              : t('accounts.warningNotice')
        let message = item?.message || '—'
        if (message === '\u8d26\u53f7\u5f53\u524d\u68c0\u6d4b\u4e3a\u5931\u6548') message = t('accounts.warningInvalidAccount')
        else if (message === '\u5c1a\u672a\u5b8c\u6210\u6709\u6548\u6027\u68c0\u6d4b') message = t('accounts.warningValidityPending')
        else message = localizeEventMessage(message, language)
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
  const { t, language } = useI18n()
  if (!sections.length) return null
  return (
    <div className="space-y-3">
      {sections.map((section: any) => (
        <div key={section?.key || section?.title} className="border-2 border-[var(--border)] bg-[var(--bg-hover)] p-3">
          <div className="text-xs font-semibold text-[var(--text-primary)]">
            {section?.title ? localizeEventMessage(section.title, language) : t('accounts.detailSection')}
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            {(Array.isArray(section?.items) ? section.items : []).map((item: any, index: number) => (
              <div key={`${item?.title || 'item'}-${index}`} className="border-2 border-[var(--border)] bg-black/20 p-3">
                <div className="text-xs font-semibold text-[var(--text-primary)]">
                  {item?.title ? localizeEventMessage(item.title, language) : '-'}
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-[var(--text-secondary)]">
                  {(Array.isArray(item?.metrics) ? item.metrics : []).map((metric: any) => (
                    <div key={metric?.key || metric?.label}>
                      <span className="text-[var(--text-muted)]">
                        {metric?.label ? localizeEventMessage(metric.label, language) : '-'}:{' '}
                      </span>
                      <span>{formatResultValue(metric?.value, language)}</span>
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

// Row actions
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
  const { t, language } = useI18n()
  const { trackJob } = useLiveJobs()
  const [running, setRunning] = useState(false)

  return (
    <div className="flex min-w-[120px] items-center justify-end gap-1.5 whitespace-nowrap">
      <button type="button" onClick={onDetail} className="table-action-btn">
        {t('accounts.detail')}
      </button>
      <button
        type="button"
        className="table-action-btn"
        disabled={running}
        title={t('accounts.checkAccountTitle')}
        onClick={() => {
          setRunning(true)
          apiFetch(`/accounts/${acc.id}/check`, { method: 'POST' })
            .then((resp) => {
              setRunning(false)
              if (resp?.task_id) {
                trackJob(
                  {
                    taskId: resp.task_id,
                    title: t('accounts.checkTaskTitle', { email: acc.email }),
                    source: 'batch',
                  },
                  { force: true },
                )
              }
            })
            .catch((e: any) => {
              setRunning(false)
              window.alert(localizeEventMessage(e?.message || t('accounts.checkRequestFailed'), language))
            })
        }}
      >
        {running ? '…' : t('accounts.checkOne')}
      </button>
      <button
        type="button"
        className="table-action-btn table-action-btn-danger"
        onClick={() => {
          if (!confirm(t('accounts.deleteAccountConfirm', { email: acc.email }))) return
          apiFetch(`/accounts/${acc.id}`, { method: 'DELETE' }).then(onDelete)
        }}
      >
        {t('common.delete')}
      </button>
    </div>
  )
}

// Account details modal
function normalizeLifecycle(value: string) {
  return value === 'invalid' ? 'invalid' : 'registered'
}

function lifecycleLabel(value: string, language: 'zh-CN' | 'en-US') {
  return translateAccountStatus(value, language)
}

export function DetailModal({ acc, onClose, onSave }: { acc: any; onClose: () => void; onSave: () => void }) {
  const { t, language } = useI18n()
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
              <span className={cn('xy-lamp', statusLamp)}>
                {translateAccountStatus(displayStatus, language)}
              </span>
              <span className="xy-lamp xy-lamp-cyan">
                {lifecycleLabel(normalizeLifecycle(getLifecycleStatus(acc)), language)}
              </span>
              <span className="xy-lamp">
                {acc.plan_name || overview.plan_name || overview.plan || translateAccountStatus(planState, language) || t('accounts.unknownPlan')}
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
                  { id: 'overview' as const, label: t('accounts.tabOverview') },
                  { id: 'secrets' as const, label: t('accounts.tabCredentials') },
                  { id: 'edit' as const, label: t('accounts.tabEdit') },
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
                <span>{t('accounts.validity')}</span>
                <span>{translateAccountStatus(validity, language) || '—'}</span>
              </div>
              <div className="xy-kv">
                <span>{t('common.password')}</span>
                <span className="max-w-[120px] truncate break-all">{acc.password || '—'}</span>
              </div>
              {verificationMailbox?.email ? (
                <div className="break-all text-[11px] text-[var(--text-muted)]">
                  {t('accounts.verificationMailbox')} · {verificationMailbox.email}
                </div>
              ) : null}
              {copied ? (
                <div className="text-[11px] text-[var(--ok)]">
                  {t('accounts.copied', { label: copied })}
                </div>
              ) : null}
            </div>
          </aside>

          <div className="min-h-0 overflow-y-auto p-4">
            {tab === 'overview' && (
              <div className="space-y-4">
                <section className="border-2 border-[var(--border)] bg-[var(--bg-input)] p-3">
                  <div className="mb-2 text-[12px] font-semibold text-[var(--text-secondary)]">
                    {t('accounts.keyInfo')}
                  </div>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {[
                      [t('common.email'), acc.email],
                      [t('accounts.plan'), acc.plan_name || overview.plan_name || overview.plan || translateAccountStatus(planState, language) || '—'],
                      [t('accounts.lifecycle'), lifecycleLabel(normalizeLifecycle(getLifecycleStatus(acc)), language)],
                      [t('accounts.validity'), translateAccountStatus(validity, language) || '—'],
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
                    <div className="mb-2 text-[12px] font-semibold text-[var(--text-secondary)]">
                      {t('accounts.metrics')}
                    </div>
                    <div className="flex flex-col gap-1.5">
                      {[...primaryMetrics, ...secondaryMetrics.slice(0, 6)].map((metric: any) => (
                        <div
                          key={metric.key || metric.label}
                          className="flex items-center justify-between gap-3 border-2 border-[var(--border)] bg-[var(--bg-pane)] px-3 py-2"
                        >
                          <div className="min-w-0 text-[12px] text-[var(--text-muted)]">
                            {metric?.label ? localizeEventMessage(metric.label, language) : '-'}
                            {metric?.sub ? ` · ${localizeEventMessage(metric.sub, language)}` : ''}
                          </div>
                          <div className="shrink-0 font-[family-name:var(--font-mono)] text-[13px] font-semibold">
                            {formatResultValue(metric?.value, language)}
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
                        {badge?.label ? localizeEventMessage(badge.label, language) : '-'}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}

            {tab === 'secrets' && (
              <div className="space-y-3">
                {platformCredentials.length === 0 && providerAccounts.length === 0 ? (
                  <div className="empty-state-panel">{t('accounts.noCredentials')}</div>
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
                        {t('accounts.copy')}
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
                                  {t('accounts.copy')}
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
                  <div className="mb-2 text-[12px] font-semibold text-[var(--text-secondary)]">
                    {t('common.status')}
                  </div>
                  <div className="flex gap-2">
                    {([
                      { value: 'registered', label: t('accounts.registered') },
                      { value: 'invalid', label: t('accounts.invalidStatus') },
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
                  <span className="text-[12px] font-semibold text-[var(--text-secondary)]">
                    {t('accounts.primaryCredential')}
                  </span>
                  <textarea
                    value={form.primary_token}
                    onChange={(e) => setForm((f) => ({ ...f, primary_token: e.target.value }))}
                    rows={4}
                    className="control-surface control-surface-mono resize-y"
                    spellCheck={false}
                  />
                </label>

                <label className="block space-y-1">
                  <span className="text-[12px] font-semibold text-[var(--text-secondary)]">
                    {t('accounts.trialLink')}
                  </span>
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
                      title: t('accounts.checkTaskTitle', { email: acc.email }),
                      source: 'batch',
                    },
                    { force: true },
                  )
                  onClose()
                }
              } catch (e: any) {
                window.alert(localizeEventMessage(e?.message || t('accounts.checkRequestFailed'), language))
              } finally {
                setChecking(false)
              }
            }}
          >
            {checking ? t('accounts.checking') : t('accounts.checkOne')}
          </Button>
          <Button onClick={save} disabled={saving || checking} className="flex-1">
            {saving ? t('common.saving') : t('common.save')}
          </Button>
          <Button variant="outline" onClick={onClose} className="flex-1" disabled={saving || checking}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    </div>
  )
}

function cn(...parts: Array<string | false | null | undefined>) {
  return parts.filter(Boolean).join(' ')
}

// Import modal
export function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { t, language } = useI18n()
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const submit = async () => {
    setLoading(true)
    try {
      const lines = text.trim().split('\n').filter(Boolean)
      const res = await apiFetch('/accounts/import', { method: 'POST', body: JSON.stringify({ platform: 'chatgpt', lines }) })
      setResult(t('accounts.importSuccess', { count: res.created })); onDone()
    } catch (e: any) {
      setResult(t('accounts.importFailed', { reason: localizeEventMessage(e?.message || t('common.error'), language) }))
    } finally { setLoading(false) }
  }
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-sm p-6" onClick={e => e.stopPropagation()}>
        <h2 className="text-base font-semibold text-[var(--text-primary)] mb-2">{t('accounts.batchImport')}</h2>
        <p className="text-xs text-[var(--text-muted)] mb-3">
          {t('accounts.importFormatHelp')} <code className="bg-[var(--bg-hover)] px-1 rounded">email password [cashier_url]</code>
        </p>
        <textarea value={text} onChange={e => setText(e.target.value)} rows={8}
          className="control-surface control-surface-mono resize-none mb-3" />
        {result && <p className="text-sm text-emerald-400 mb-3">{result}</p>}
        <div className="flex gap-2">
          <Button onClick={submit} disabled={loading} className="flex-1">
            {loading ? t('accounts.importing') : t('accounts.import')}
          </Button>
          <Button variant="outline" onClick={onClose} className="flex-1">{t('common.cancel')}</Button>
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
  const { t, language } = useI18n()
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState<string | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const hasSelection = selectedIds.length > 0
  const exportFormats = useMemo(() => getAccountExportFormats(language), [language])

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
      window.alert(localizeEventMessage(e?.message || t('accounts.exportFailed'), language))
    } finally {
      setLoading(null)
    }
  }

  if (layout === 'grid') {
    return (
      <div className="grid gap-2 sm:grid-cols-3">
        {exportFormats.map((option) => (
          <button
            key={option.key}
            type="button"
            disabled={total === 0 || !!loading}
            onClick={() => doExport(option.key)}
            className="border border-[var(--border)] bg-[var(--bg-input)] px-3 py-3 text-left transition-colors hover:border-[var(--accent-edge)] hover:bg-[var(--accent-soft)] disabled:opacity-45"
          >
            <div className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.08em] text-[var(--accent-strong)]">
              {loading === option.key ? t('accounts.writing') : option.key}
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
          ? t('accounts.scopeSelected', { count: selectedIds.length })
          : t('accounts.scopeCurrent')}
      </div>
      {exportFormats.map((option) => (
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

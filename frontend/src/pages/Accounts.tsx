import { useEffect, useState, useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch, cn } from '@/lib/utils'
import { formatDateTime, localizeEventMessage, translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { Button } from '@/components/ui/button'
import {
  RefreshCw,
  Copy,
  ExternalLink,
  Upload,
  Trash2,
  Zap,
  Play,
  ListTree,
  ArrowLeftRight,
  X,
} from 'lucide-react'
import {
  STATUS_VARIANT,
  getAccountOverview,
  getVerificationMailbox,
  getLifecycleStatus,
  getDisplayStatus,
  getPlanState,
  getValidityStatus,
  getPrimaryMetrics,
  getDisplayBadges,
  getCashierUrl,
  getCompactStatusMeta,
  emailApiLine,
  copyText,
} from '@/features/accounts/helpers'
import {
  RegisterModal,
  DetailModal,
  ImportModal,
  ExportMenu,
  ActionMenu,
} from '@/features/accounts/modals'
import { useLiveJobs } from '@/lib/live-jobs'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { getTaskStatusText } from '@/lib/tasks'

type Mode = 'library' | 'register' | 'io'

export default function Accounts() {
  const { t, language } = useI18n()
  const {
    jobs,
    activeTaskId,
    setActiveTaskId,
    trackJob,
    updateJobStatus,
    dismissJob,
  } = useLiveJobs()
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = 'chatgpt'

  const modeParam = searchParams.get('mode')
  const mode: Mode =
    modeParam === 'register' || modeParam === 'io' || modeParam === 'library'
      ? modeParam
      : 'library'

  const setMode = (m: Mode) => {
    const next = new URLSearchParams(searchParams)
    next.set('mode', m)
    setSearchParams(next, { replace: true })
  }

  const [accounts, setAccounts] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [detail, setDetail] = useState<any | null>(null)
  const [showImport, setShowImport] = useState(false)
  const [showRegister, setShowRegister] = useState(false)
  const [platformsMap, setPlatformsMap] = useState<Record<string, any>>({})
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [batchRefreshing, setBatchRefreshing] = useState(false)

  useEffect(() => {
    getPlatforms()
      .then((list: any[]) => {
        const map: Record<string, any> = {}
        list.forEach((p) => {
          map[p.name] = p
        })
        setPlatformsMap(map)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 400)
    return () => clearTimeout(timer)
  }, [search])

  useEffect(() => {
    setSelectedIds(new Set())
  }, [filterStatus, debouncedSearch])

  const load = useCallback(
    async (p = tab, s = debouncedSearch, fs = filterStatus) => {
      setLoading(true)
      try {
        const params = new URLSearchParams({ platform: p, page: '1', page_size: '100' })
        if (s) params.set('email', s)
        if (fs) params.set('status', fs)
        const data = await apiFetch(`/accounts?${params}`)
        setAccounts(data.items)
        setTotal(data.total)
      } finally {
        setLoading(false)
      }
    },
    [tab, debouncedSearch, filterStatus],
  )

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    setSelectedIds((prev) => {
      const visible = new Set(accounts.map((acc) => acc.id))
      return new Set([...prev].filter((id) => visible.has(id)))
    })
  }, [accounts])

  useEffect(() => {
    if (mode === 'register') setShowRegister(true)
  }, [mode])

  const pageIds = accounts.map((acc) => acc.id)
  const allSelectedOnPage = pageIds.length > 0 && pageIds.every((id) => selectedIds.has(id))
  const selectedCount = selectedIds.size

  const toggleOne = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = () => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (allSelectedOnPage) pageIds.forEach((id) => next.delete(id))
      else pageIds.forEach((id) => next.add(id))
      return next
    })
  }

  const platformLabel = platformsMap[tab]?.display_name || 'ChatGPT'
  const visibleTrial = accounts.filter((acc) => getPlanState(acc) === 'trial').length
  const visibleSubscribed = accounts.filter((acc) => getPlanState(acc) === 'subscribed').length
  const visibleInvalid = accounts.filter(
    (acc) => getValidityStatus(acc) === 'invalid' || getLifecycleStatus(acc) === 'invalid',
  ).length

  const modes = useMemo(
    () =>
      [
        { id: 'library' as const, label: t('accounts.modeLibrary'), icon: ListTree },
        { id: 'register' as const, label: t('accounts.modeRegister'), icon: Play },
        { id: 'io' as const, label: t('accounts.modeIo'), icon: ArrowLeftRight },
      ] as const,
    [t],
  )

  const lampForStatus = (status: string) => {
    const v = STATUS_VARIANT[status]
    if (v === 'success') return 'xy-lamp-ok'
    if (v === 'warning') return 'xy-lamp-warn'
    if (v === 'danger') return 'xy-lamp-danger'
    if (v === 'default') return 'xy-lamp-accent'
    return ''
  }

  // Keep account-check jobs in the side dock; registration jobs live on the Jobs page.
  const checkJobs = jobs.filter((j) => j.source === 'batch')
  const activeCheck =
    checkJobs.find((j) => j.taskId === activeTaskId) || checkJobs[0] || null
  const recentCheckJobs = checkJobs.slice(0, 6)

  const startCheck = async (opts: { ids?: number[]; selectAll?: boolean; title: string }) => {
    setBatchRefreshing(true)
    try {
      const res = await apiFetch('/accounts/check-all', {
        method: 'POST',
        body: JSON.stringify(
          opts.selectAll
            ? { platform: tab, ids: [], select_all: true }
            : { platform: tab, ids: opts.ids || [], select_all: false },
        ),
      })
      if (res?.task_id) {
        trackJob(
          {
            taskId: res.task_id,
            title: opts.title,
            source: 'batch',
          },
          { force: true },
        )
        setMode('library')
      }
    } catch (e: any) {
      window.alert(localizeEventMessage(e?.message || t('accounts.checkRequestFailed'), language))
    } finally {
      setBatchRefreshing(false)
    }
  }

  return (
    <div className="xy-page">
      {detail && (
        <DetailModal
          acc={detail}
          onClose={() => setDetail(null)}
          onSave={() => {
            setDetail(null)
            load()
          }}
        />
      )}
      {showImport && (
        <ImportModal
          onClose={() => setShowImport(false)}
          onDone={() => {
            setShowImport(false)
            load()
          }}
        />
      )}
      {showRegister && (
        <RegisterModal
          platformMeta={platformsMap[tab]}
          onClose={() => {
            setShowRegister(false)
            if (mode === 'register') setMode('library')
          }}
          onDone={() => load()}
        />
      )}

      <div className="xy-strip">
        <div>
          <div className="xy-k">{platformLabel}</div>
          <h1 className="xy-h1">{t('accounts.pageTitle')}</h1>
          <p className="xy-sub">{t('accounts.pageSubtitle')}</p>
        </div>
        <div className="xy-switchrow">
          {modes.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              className={cn('xy-sw', mode === id && 'xy-sw-on')}
              onClick={() => {
                setMode(id)
                if (id === 'register') setShowRegister(true)
              }}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <span className="xy-lamp">{t('accounts.poolBadge', { count: total })}</span>
        {visibleTrial > 0 && <span className="xy-lamp xy-lamp-warn">{t('accounts.trialBadge', { count: visibleTrial })}</span>}
        {visibleSubscribed > 0 && (
          <span className="xy-lamp xy-lamp-ok">{t('accounts.subscribedBadge', { count: visibleSubscribed })}</span>
        )}
        {visibleInvalid > 0 && (
          <span className="xy-lamp xy-lamp-danger">{t('accounts.invalidBadge', { count: visibleInvalid })}</span>
        )}
        {selectedCount > 0 && (
          <span className="xy-lamp xy-lamp-cyan">{t('accounts.selectedBadge', { count: selectedCount })}</span>
        )}
        {(() => {
          const reg = jobs.filter((j) => j.source === 'register').length
          const chk = jobs.filter((j) => j.source === 'batch').length
          const run = jobs.filter(
            (j) =>
              !j.status ||
              ['running', 'claimed', 'pending', 'cancel_requested'].includes(j.status),
          ).length
          return (
            <>
              {run > 0 && <span className="xy-lamp xy-lamp-mag">{t('accounts.runningBadge', { count: run })}</span>}
              {reg > 0 && <span className="xy-lamp xy-lamp-ok">{t('accounts.registerBadge', { count: reg })}</span>}
              {chk > 0 && <span className="xy-lamp xy-lamp-warn">{t('accounts.checkBadge', { count: chk })}</span>}
            </>
          )
        })()}
      </div>

      {mode === 'register' && (
        <section className="xy-runbar">
          <div>
            <div className="xy-runbar-title">{t('accounts.registerTitle')}</div>
            <div className="xy-runbar-desc">
              {t('accounts.registerDesc')}
            </div>
            <div className="xy-runbar-actions">
              <Button onClick={() => setShowRegister(true)}>
                <Play className="mr-2 h-3.5 w-3.5" />
                {t('accounts.openRegister')}
              </Button>
              <Button variant="outline" onClick={() => setMode('library')}>
                {t('accounts.backToPool')}
              </Button>
            </div>
          </div>
          <div className="xy-runbar-side">
            <div className="xy-kv">
              <span>{t('common.platform')}</span>
              <span>ChatGPT</span>
            </div>
            <div className="xy-kv">
              <span>{t('accounts.inventory')}</span>
              <span>{total}</span>
            </div>
            <div className="xy-kv">
              <span>{t('accounts.description')}</span>
              <span>{t('accounts.refreshOnSuccess')}</span>
            </div>
          </div>
        </section>
      )}

      {mode === 'io' && (
        <section className="space-y-3">
          <div className="xy-runbar">
            <div>
              <div className="xy-runbar-title">{t('accounts.ioTitle')}</div>
              <div className="xy-runbar-desc">
                {t('accounts.ioDesc')}
              </div>
            </div>
            <div className="xy-runbar-side">
              <div className="xy-kv">
                <span>{t('accounts.inventory')}</span>
                <span>{total}</span>
              </div>
              <div className="xy-kv">
                <span>{t('accounts.selectedLabel')}</span>
                <span>{selectedCount}</span>
              </div>
              <div className="xy-kv">
                <span>{t('accounts.filterLabel')}</span>
                <span>{filterStatus ? translateAccountStatus(filterStatus, language) : t('accounts.all')}</span>
              </div>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="xy-panel">
              <div className="xy-panel-h">
                <h2 className="xy-panel-t">{t('accounts.importTitle')}</h2>
              </div>
              <div className="xy-panel-b space-y-3">
                <p className="text-[13px] text-[var(--text-muted)]">
                  {t('accounts.importFormat')}
                </p>
                <Button onClick={() => setShowImport(true)}>
                  <Upload className="mr-2 h-3.5 w-3.5" />
                  {t('accounts.pasteImport')}
                </Button>
              </div>
            </div>
            <div className="xy-panel">
              <div className="xy-panel-h">
                <h2 className="xy-panel-t">{t('accounts.exportTitle')}</h2>
                <span className="xy-lamp xy-lamp-accent">
                  {selectedCount > 0 ? t('accounts.selectedBadge', { count: selectedCount }) : t('accounts.exportCurrent')}
                </span>
              </div>
              <div className="xy-panel-b space-y-3">
                <p className="text-[13px] text-[var(--text-muted)]">
                  {t('accounts.exportFormats')}
                </p>
                <ExportMenu
                  total={total}
                  statusFilter={filterStatus}
                  searchFilter={debouncedSearch}
                  selectedIds={[...selectedIds]}
                  layout="grid"
                />
              </div>
            </div>
          </div>
        </section>
      )}

      {mode === 'library' && (
        <div className="xy-bay-logs">
          <div className="xy-ledger overflow-visible">
            <div className="xy-ledger-tools overflow-visible">
              <input
                type="text"
                placeholder={t('accounts.searchPlaceholder')}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="control-surface control-surface-compact min-w-[160px] flex-1 max-w-xs"
              />
              <select
                value={filterStatus}
                onChange={(e) => setFilterStatus(e.target.value)}
                className="control-surface control-surface-compact appearance-none"
              >
                <option value="">{t('accounts.allStatuses')}</option>
                <option value="registered">{t('accounts.registered')}</option>
                <option value="invalid">{t('accounts.invalidStatus')}</option>
              </select>
              <label className="flex items-center gap-1.5 text-[12px] text-[var(--text-muted)]">
                <input
                  type="checkbox"
                  checked={allSelectedOnPage}
                  onChange={togglePage}
                  className="checkbox-accent"
                />
                {t('accounts.selectAllPage')}
              </label>
              <div className="ml-auto flex flex-wrap gap-1.5">
                <ExportMenu
                  total={total}
                  statusFilter={filterStatus}
                  searchFilter={debouncedSearch}
                  selectedIds={[...selectedIds]}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={batchRefreshing || loading || total === 0}
                  title={
                    selectedCount > 0
                      ? t('accounts.checkSelectedTitle', { count: selectedCount })
                      : t('accounts.checkAllTitle')
                  }
                  onClick={async () => {
                    const useSelection = selectedCount > 0
                    if (
                      !confirm(
                        useSelection
                          ? t('accounts.checkSelectedConfirm', { count: selectedCount })
                          : t('accounts.checkAllConfirm', { count: total }),
                      )
                    ) {
                      return
                    }
                    await startCheck({
                      ids: useSelection ? [...selectedIds] : undefined,
                      selectAll: !useSelection,
                      title: useSelection
                        ? t('accounts.checkSelectedTask', { count: selectedCount })
                        : t('accounts.checkAllTask', { count: total }),
                    })
                  }}
                >
                  <Zap className={`mr-1 h-3.5 w-3.5 ${batchRefreshing ? 'animate-pulse' : ''}`} />
                  {selectedCount > 0 ? t('accounts.checkSelectedButton', { count: selectedCount }) : t('accounts.checkAllButton')}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => load()} disabled={loading}>
                  <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
                </Button>
                {selectedCount > 0 && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={bulkDeleting}
                    className="text-[var(--danger)]"
                    onClick={async () => {
                      if (
                        !confirm(t('accounts.deleteSelectedConfirm', { count: selectedCount }))
                      )
                        return
                      setBulkDeleting(true)
                      try {
                        await Promise.allSettled(
                          [...selectedIds].map((id) =>
                            apiFetch(`/accounts/${id}`, { method: 'DELETE' }),
                          ),
                        )
                        setSelectedIds(new Set())
                        load()
                      } finally {
                        setBulkDeleting(false)
                      }
                    }}
                  >
                    <Trash2 className="mr-1 h-3.5 w-3.5" />
                    {t('common.delete')}
                  </Button>
                )}
              </div>
            </div>

            <div className="xy-ledger-wrap">
              {accounts.length === 0 ? (
                <div className="empty-state-panel m-6">
                  <div className="text-[14px] font-semibold text-[var(--text-primary)]">
                    {t('accounts.noAccounts')}
                  </div>
                  <p className="mx-auto mt-2 max-w-sm text-[13px]">
                    {t('accounts.noAccountsDesc')}
                  </p>
                  <div className="mt-4 flex justify-center gap-2">
                    <Button
                      size="sm"
                      onClick={() => {
                        setMode('register')
                        setShowRegister(true)
                      }}
                    >
                      {t('accounts.goRegister')}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setMode('io')}>
                      {t('accounts.goImport')}
                    </Button>
                  </div>
                </div>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th className="w-10">
                        <input
                          type="checkbox"
                          checked={allSelectedOnPage}
                          onChange={togglePage}
                          className="checkbox-accent"
                        />
                      </th>
                      <th>{t('common.email')}</th>
                      <th>{t('common.password')}</th>
                      <th>{t('common.status')}</th>
                      <th>{t('accounts.info')}</th>
                      <th>{t('accounts.createdAt')}</th>
                      <th className="text-right">{t('common.actions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {accounts.map((acc) => {
                      const overview = getAccountOverview(acc)
                      const verificationMailbox = getVerificationMailbox(acc)
                      const primaryMetrics = getPrimaryMetrics(acc)
                      const displayBadges = getDisplayBadges(acc)
                      const status = getDisplayStatus(acc)
                      const selected = selectedIds.has(acc.id)

                      return (
                        <tr
                          key={acc.id}
                          className={cn(selected && 'is-on')}
                          onClick={() => setDetail(acc)}
                        >
                          <td onClick={(e) => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              checked={selected}
                              onChange={() => toggleOne(acc.id)}
                              className="checkbox-accent"
                            />
                          </td>
                          <td>
                            <div className="xy-mono flex items-center gap-1.5">
                              <span className="truncate max-w-[220px]" title={acc.email}>
                                {acc.email}
                              </span>
                              <button
                                type="button"
                                className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  copyText(emailApiLine(acc.email))
                                }}
                              >
                                <Copy className="h-3 w-3" />
                              </button>
                            </div>
                            {verificationMailbox?.email && (
                              <div className="mt-0.5 text-[11px] text-[var(--text-muted)]">
                                {t('accounts.verificationMailbox')} · {verificationMailbox.email}
                              </div>
                            )}
                            {overview?.remote_email && overview.remote_email !== acc.email && (
                              <div className="mt-0.5 text-[11px] text-[var(--text-muted)]">
                                {t('accounts.remoteMailbox')} · {overview.remote_email}
                              </div>
                            )}
                            {displayBadges.length > 0 && (
                              <div className="mt-1 flex flex-wrap gap-1">
                                {displayBadges.slice(0, 2).map((b: any, i: number) => (
                                  <span key={i} className="xy-lamp">
                                    {b?.label ? localizeEventMessage(b.label, language) : '-'}
                                  </span>
                                ))}
                              </div>
                            )}
                          </td>
                          <td>
                            <div className="xy-mono flex items-center gap-1.5 text-[var(--text-muted)]">
                              <span className="max-w-[140px] truncate">
                                {acc.password}
                              </span>
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  copyText(acc.password)
                                }}
                              >
                                <Copy className="h-3 w-3" />
                              </button>
                            </div>
                          </td>
                          <td>
                            <span className={cn('xy-lamp', lampForStatus(status))}>
                              {translateAccountStatus(status, language)}
                            </span>
                          </td>
                          <td className="max-w-[180px]">
                            {primaryMetrics.length > 0 ? (
                              <div className="space-y-0.5 text-[11px] text-[var(--text-muted)]">
                                {primaryMetrics.slice(0, 2).map((m: any) => (
                                  <div key={m.key || m.label}>
                                    <span className="text-[var(--text-secondary)]">
                                      {m.label ? localizeEventMessage(m.label, language) : '-'}
                                    </span>
                                    : {m.value}
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <div
                                className="truncate text-[11px] text-[var(--text-muted)]"
                                title={getCompactStatusMeta(acc, language)}
                              >
                                {getCompactStatusMeta(acc, language)}
                              </div>
                            )}
                            {getCashierUrl(acc) && (
                              <div
                                className="mt-1 flex gap-1"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <button
                                  type="button"
                                  onClick={() => copyText(getCashierUrl(acc))}
                                  className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                                >
                                  <Copy className="h-3 w-3" />
                                </button>
                                <a
                                  href={getCashierUrl(acc)}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-[var(--text-muted)] hover:text-[var(--text-primary)]"
                                >
                                  <ExternalLink className="h-3 w-3" />
                                </a>
                              </div>
                            )}
                          </td>
                          <td className="xy-mono whitespace-nowrap text-[11px] text-[var(--text-muted)]">
                            {acc.created_at
                              ? formatDateTime(acc.created_at, language, {
                                  month: '2-digit',
                                  day: '2-digit',
                                  hour: '2-digit',
                                  minute: '2-digit',
                                  hour12: false,
                                })
                              : t('common.unknown')}
                          </td>
                          <td
                            className="text-right"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <ActionMenu
                              acc={acc}
                              onDetail={() => setDetail(acc)}
                              onDelete={() => load()}
                            />
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          <aside className="xy-logdock">
            <div className="xy-logdock-h">
              <div>
                <div className="xy-k">{t('accounts.checkLogs')}</div>
                <div className="mt-1 text-[13px] font-semibold">
                  {activeCheck ? localizeEventMessage(activeCheck.title, language) : t('accounts.checkTasksOnly')}
                </div>
              </div>
              {activeCheck ? (
                <button
                  type="button"
                  className="xy-icon-btn"
                  title={t('accounts.closeCurrentCheck')}
                  onClick={() => dismissJob(activeCheck.taskId)}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              ) : null}
            </div>
            <div className="xy-logdock-b">
              {recentCheckJobs.length > 0 ? (
                <div className="xy-joblist">
                  {recentCheckJobs.map((job) => (
                    <button
                      key={job.taskId}
                      type="button"
                      className={cn(
                        'xy-jobchip',
                        (activeCheck?.taskId || '') === job.taskId && 'is-on',
                      )}
                      onClick={() => setActiveTaskId(job.taskId)}
                    >
                      <span className="min-w-0 flex-1 truncate">{job.title}</span>
                      <span className="shrink-0 text-[10px] text-[var(--text-muted)]">
                        {job.status
                          ? getTaskStatusText(job.status, language)
                          : t('accounts.inProgress')}
                      </span>
                    </button>
                  ))}
                </div>
              ) : null}

              {activeCheck ? (
                <div className="xy-log-scroll">
                  <TaskLogPanel
                    compact
                    taskId={activeCheck.taskId}
                    onDone={(status) => {
                      updateJobStatus(activeCheck.taskId, status)
                      if (status === 'succeeded') load()
                    }}
                  />
                </div>
              ) : (
                <div className="empty-state-panel">
                  <div className="text-[13px] font-semibold text-[var(--text-primary)]">
                    {t('accounts.noCheckLogs')}
                  </div>
                  <p className="mt-2 text-[12px]">
                    {t('accounts.checkLogsHint')}
                  </p>
                </div>
              )}
            </div>
          </aside>
        </div>
      )}
    </div>
  )
}

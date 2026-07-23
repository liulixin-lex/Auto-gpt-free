import { useEffect, useState, useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getPlatforms } from '@/lib/app-data'
import { apiFetch, cn } from '@/lib/utils'
import { formatDateTime, translateAccountStatus } from '@/lib/i18n'
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
  ActionResultModal,
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
  const [actionResult, setActionResult] = useState<{ title: string; payload: any } | null>(null)
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

  // 号池右侧只展示检测类任务；注册/其它动作去「任务日志」
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
      window.alert(e?.message || '检测请求失败')
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
      {actionResult && (
        <ActionResultModal
          title={actionResult.title}
          payload={actionResult.payload}
          onClose={() => setActionResult(null)}
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
        <span className="xy-lamp">号池 {total}</span>
        {visibleTrial > 0 && <span className="xy-lamp xy-lamp-warn">试用 {visibleTrial}</span>}
        {visibleSubscribed > 0 && (
          <span className="xy-lamp xy-lamp-ok">已订阅 {visibleSubscribed}</span>
        )}
        {visibleInvalid > 0 && (
          <span className="xy-lamp xy-lamp-danger">失效 {visibleInvalid}</span>
        )}
        {selectedCount > 0 && (
          <span className="xy-lamp xy-lamp-cyan">已选 {selectedCount}</span>
        )}
        {(() => {
          const reg = jobs.filter((j) => j.source === 'register').length
          const ops = jobs.filter((j) => j.source === 'ops').length
          const chk = jobs.filter((j) => j.source === 'batch').length
          const act = jobs.filter((j) => j.source === 'action').length
          const run = jobs.filter(
            (j) =>
              !j.status ||
              ['running', 'claimed', 'pending', 'cancel_requested'].includes(j.status),
          ).length
          return (
            <>
              {run > 0 && <span className="xy-lamp xy-lamp-mag">执行中 {run}</span>}
              {ops > 0 && <span className="xy-lamp xy-lamp-accent">运维 {ops}</span>}
              {reg > 0 && <span className="xy-lamp xy-lamp-ok">注册 {reg}</span>}
              {chk > 0 && <span className="xy-lamp xy-lamp-warn">检测 {chk}</span>}
              {act > 0 && <span className="xy-lamp xy-lamp-cyan">动作 {act}</span>}
            </>
          )
        })()}
      </div>

      {mode === 'register' && (
        <section className="xy-runbar">
          <div>
            <div className="xy-runbar-title">注册账号</div>
            <div className="xy-runbar-desc">
              填写数量、邮箱通道和执行方式后开始。进度与日志在右侧实时显示。
            </div>
            <div className="xy-runbar-actions">
              <Button onClick={() => setShowRegister(true)}>
                <Play className="mr-2 h-3.5 w-3.5" />
                打开注册
              </Button>
              <Button variant="outline" onClick={() => setMode('library')}>
                返回号池
              </Button>
            </div>
          </div>
          <div className="xy-runbar-side">
            <div className="xy-kv">
              <span>平台</span>
              <span>ChatGPT</span>
            </div>
            <div className="xy-kv">
              <span>库存</span>
              <span>{total}</span>
            </div>
            <div className="xy-kv">
              <span>说明</span>
              <span>成功后自动刷新</span>
            </div>
          </div>
        </section>
      )}

      {mode === 'io' && (
        <section className="space-y-3">
          <div className="xy-runbar">
            <div>
              <div className="xy-runbar-title">导入 / 导出</div>
              <div className="xy-runbar-desc">
                导入：粘贴账号文本。导出：JSON / Sub2API（Agent Identity 或 OAuth 回退）/ CPA token。
              </div>
            </div>
            <div className="xy-runbar-side">
              <div className="xy-kv">
                <span>库存</span>
                <span>{total}</span>
              </div>
              <div className="xy-kv">
                <span>已选</span>
                <span>{selectedCount}</span>
              </div>
              <div className="xy-kv">
                <span>筛选</span>
                <span>{filterStatus || '全部'}</span>
              </div>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div className="xy-panel">
              <div className="xy-panel-h">
                <h2 className="xy-panel-t">导入</h2>
              </div>
              <div className="xy-panel-b space-y-3">
                <p className="text-[13px] text-[var(--text-muted)]">
                  每行：邮箱 密码 [支付链接]
                </p>
                <Button onClick={() => setShowImport(true)}>
                  <Upload className="mr-2 h-3.5 w-3.5" />
                  粘贴导入
                </Button>
              </div>
            </div>
            <div className="xy-panel">
              <div className="xy-panel-h">
                <h2 className="xy-panel-t">导出</h2>
                <span className="xy-lamp xy-lamp-accent">
                  {selectedCount > 0 ? `已选 ${selectedCount}` : '按当前筛选'}
                </span>
              </div>
              <div className="xy-panel-b space-y-3">
                <p className="text-[13px] text-[var(--text-muted)]">
                  支持 json · sub2api-agent-identity · cpa
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
                placeholder="搜索邮箱…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="control-surface control-surface-compact min-w-[160px] flex-1 max-w-xs"
              />
              <select
                value={filterStatus}
                onChange={(e) => setFilterStatus(e.target.value)}
                className="control-surface control-surface-compact appearance-none"
              >
                <option value="">全部状态</option>
                <option value="registered">已注册</option>
                <option value="invalid">已失效</option>
              </select>
              <label className="flex items-center gap-1.5 text-[12px] text-[var(--text-muted)]">
                <input
                  type="checkbox"
                  checked={allSelectedOnPage}
                  onChange={togglePage}
                  className="checkbox-accent"
                />
                本页全选
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
                      ? `检测已选 ${selectedCount} 个账号`
                      : '检测当前全部账号'
                  }
                  onClick={async () => {
                    const useSelection = selectedCount > 0
                    if (
                      !confirm(
                        useSelection
                          ? `确认检测已选的 ${selectedCount} 个账号？日志在右侧显示。`
                          : `未勾选时将检测当前平台全部账号（约 ${total} 个）。日志在右侧显示，是否继续？`,
                      )
                    ) {
                      return
                    }
                    await startCheck({
                      ids: useSelection ? [...selectedIds] : undefined,
                      selectAll: !useSelection,
                      title: useSelection
                        ? `检测已选 ${selectedCount} 个`
                        : `检测全部 ${total} 个`,
                    })
                  }}
                >
                  <Zap className={`mr-1 h-3.5 w-3.5 ${batchRefreshing ? 'animate-pulse' : ''}`} />
                  {selectedCount > 0 ? `检测已选(${selectedCount})` : '检测全部'}
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
                    删除
                  </Button>
                )}
              </div>
            </div>

            <div className="xy-ledger-wrap">
              {accounts.length === 0 ? (
                <div className="empty-state-panel m-6">
                  <div className="text-[14px] font-semibold text-[var(--text-primary)]">
                    暂无账号
                  </div>
                  <p className="mx-auto mt-2 max-w-sm text-[13px]">
                    可以先注册一批，或从导入页粘贴已有账号。
                  </p>
                  <div className="mt-4 flex justify-center gap-2">
                    <Button
                      size="sm"
                      onClick={() => {
                        setMode('register')
                        setShowRegister(true)
                      }}
                    >
                      去注册
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setMode('io')}>
                      去导入
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
                      <th>邮箱</th>
                      <th>密码</th>
                      <th>状态</th>
                      <th>信息</th>
                      <th>创建时间</th>
                      <th className="text-right">操作</th>
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
                                验证邮箱 · {verificationMailbox.email}
                              </div>
                            )}
                            {overview?.remote_email && overview.remote_email !== acc.email && (
                              <div className="mt-0.5 text-[11px] text-[var(--text-muted)]">
                                远程邮箱 · {overview.remote_email}
                              </div>
                            )}
                            {displayBadges.length > 0 && (
                              <div className="mt-1 flex flex-wrap gap-1">
                                {displayBadges.slice(0, 2).map((b: any, i: number) => (
                                  <span key={i} className="xy-lamp">
                                    {b?.label}
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
                                    <span className="text-[var(--text-secondary)]">{m.label}</span>
                                    : {m.value}
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <div
                                className="truncate text-[11px] text-[var(--text-muted)]"
                                title={getCompactStatusMeta(acc)}
                              >
                                {getCompactStatusMeta(acc)}
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
                              : '—'}
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
                <div className="xy-k">检测日志</div>
                <div className="mt-1 text-[13px] font-semibold">
                  {activeCheck ? activeCheck.title : '仅显示检测任务'}
                </div>
              </div>
              {activeCheck ? (
                <button
                  type="button"
                  className="xy-icon-btn"
                  title="关闭当前检测"
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
                          : '进行中'}
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
                    暂无检测日志
                  </div>
                  <p className="mt-2 text-[12px]">
                    点「检测全部 / 检测已选 / 行内检测」后，进度会出现在这里。
                    注册任务请到「任务日志」查看。
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

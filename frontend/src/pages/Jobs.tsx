import { Link } from 'react-router-dom'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { TaskLogPanel } from '@/components/tasks/TaskLogPanel'
import { Button } from '@/components/ui/button'
import { useLiveJobs, type LiveJob } from '@/lib/live-jobs'
import { cn, apiFetch } from '@/lib/utils'
import { getTaskStatusText } from '@/lib/tasks'
import { useI18n } from '@/lib/i18n-context'
import {
  Activity,
  Trash2,
  Radio,
  RefreshCw,
  Radar,
  Square,
  Play,
  OctagonX,
} from 'lucide-react'

type ServerTask = {
  id: string
  task_id?: string
  type?: string
  status?: string
  progress?: string
  success?: number
  error_count?: number
  created_at?: string
  started_at?: string
  finished_at?: string
}

type OpsStatus = {
  running?: boolean
  cycle_in_progress?: boolean
  auto_probe_enabled?: boolean
  auto_delete_remote_enabled?: boolean
  auto_replenish_enabled?: boolean
  auto_register_enabled?: boolean
  auto_upload_enabled?: boolean
  sync_target?: string
  auto_probe_interval_minutes?: number
  auto_ops_interval_seconds?: number
  auto_register_concurrency?: number
  last_probe_at?: string
  last_probe_result?: string
  last_replenish_at?: string
  last_cycle_at?: string
  next_cycle_at?: string
  register_task_active?: boolean
  active_register_task_ids?: string[]
  recent_logs?: Array<{ at: string; level: string; message: string }>
}

type JobSource = LiveJob['source']

const GROUP_ORDER: JobSource[] = ['ops', 'register', 'batch', 'action', 'other']

function taskTitle(task: ServerTask, t: (k: any) => string): string {
  const typ = task.type || 'other'
  if (typ === 'register') return t('jobs.typeRegister')
  if (typ === 'auto_ops') return t('jobs.typeAutoOpsLive')
  if (typ === 'account_check_all') return t('jobs.typeCheck')
  if (typ === 'platform_action') return t('jobs.typeAction')
  return typ
}

function taskSource(task: ServerTask): JobSource {
  if (task.type === 'register') return 'register'
  if (task.type === 'auto_ops') return 'ops'
  if (task.type === 'account_check_all') return 'batch'
  if (task.type === 'platform_action') return 'action'
  return 'other'
}

function groupLabel(source: JobSource, t: (k: any) => string): string {
  if (source === 'ops') return t('jobs.groupOps')
  if (source === 'register') return t('jobs.groupRegister')
  if (source === 'batch') return t('jobs.groupCheck')
  if (source === 'action') return t('jobs.groupAction')
  return t('jobs.groupOther')
}

function fmtTime(iso?: string) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export default function Jobs() {
  const { language, t } = useI18n()
  const {
    jobs,
    activeTaskId,
    setActiveTaskId,
    updateJobStatus,
    dismissJob,
    clearTerminal,
    trackJob,
    isJobHidden,
  } = useLiveJobs()

  const [serverTasks, setServerTasks] = useState<ServerTask[]>([])
  const [ops, setOps] = useState<OpsStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [ctrlBusy, setCtrlBusy] = useState(false)
  const [ctrlMsg, setCtrlMsg] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [taskRes, status] = await Promise.all([
        apiFetch('/tasks?limit=80'),
        apiFetch('/auto-ops/status').catch(() => null),
      ])
      const items: ServerTask[] = Array.isArray(taskRes?.items) ? taskRes.items : []
      setServerTasks(items)
      if (status) setOps(status)

      for (const item of items.slice(0, 40)) {
        const id = item.id || item.task_id
        if (!id || isJobHidden(id)) continue
        trackJob({
          taskId: id,
          title: taskTitle(item, t),
          source: taskSource(item),
          status: item.status || null,
          startedAt: item.started_at
            ? Date.parse(item.started_at)
            : item.created_at
              ? Date.parse(item.created_at)
              : Date.now(),
        })
      }
    } catch {
      // keep previous
    } finally {
      setLoading(false)
    }
  }, [t, trackJob, isJobHidden])

  const runOpsControl = useCallback(
    async (path: string, body?: Record<string, unknown>) => {
      setCtrlBusy(true)
      setCtrlMsg(t('jobs.ctrlWorking'))
      try {
        await apiFetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body ? JSON.stringify(body) : undefined,
        })
        setCtrlMsg(t('jobs.ctrlDone'))
        await refresh()
      } catch {
        setCtrlMsg(t('jobs.ctrlFail'))
      } finally {
        setCtrlBusy(false)
        window.setTimeout(() => setCtrlMsg(''), 2500)
      }
    },
    [t, refresh],
  )

  useEffect(() => {
    refresh()
    const id = window.setInterval(refresh, 3000)
    return () => window.clearInterval(id)
  }, [refresh])

  // Collapse many auto_ops history tasks into a single rolling window.
  // Respect hide list so "清理已结束" stays effective after server re-poll.
  const merged = useMemo(() => {
    const map = new Map(
      jobs.filter((j) => !isJobHidden(j.taskId)).map((j) => [j.taskId, j]),
    )
    for (const st of serverTasks) {
      const id = st.id || st.task_id
      if (!id || isJobHidden(id)) continue
      if (!map.has(id)) {
        map.set(id, {
          taskId: id,
          title: taskTitle(st, t),
          source: taskSource(st),
          startedAt: st.created_at ? Date.parse(st.created_at) : Date.now(),
          status: st.status,
        })
      } else {
        const cur = map.get(id)!
        if (st.status && cur.status !== st.status) {
          map.set(id, { ...cur, status: st.status })
        }
      }
    }

    const all = Array.from(map.values()).sort((a, b) => b.startedAt - a.startedAt)
    const opsJobs = all.filter((j) => j.source === 'ops')
    const nonOps = all.filter((j) => j.source !== 'ops')
    // Prefer running ops session; else newest ops only once.
    const opsKeep =
      opsJobs.find((j) => !j.status || ['running', 'claimed', 'pending'].includes(j.status)) ||
      opsJobs[0]
    return opsKeep ? [opsKeep, ...nonOps] : nonOps
  }, [jobs, serverTasks, t, isJobHidden])

  const groups = useMemo(() => {
    const by: Record<string, LiveJob[]> = {}
    for (const j of merged) {
      const k = j.source || 'other'
      if (!by[k]) by[k] = []
      by[k].push(j)
    }
    return GROUP_ORDER.filter((g) => (by[g] || []).length > 0).map((g) => ({
      source: g,
      label: groupLabel(g, t),
      items: by[g],
    }))
  }, [merged, t])

  const active =
    merged.find((j) => j.taskId === activeTaskId) || merged[0] || null

  useEffect(() => {
    if (active && active.taskId !== activeTaskId) {
      setActiveTaskId(active.taskId)
    }
  }, [active, activeTaskId, setActiveTaskId])

  const handleDone = (status: string) => {
    if (!active) return
    // Persistent auto_ops session stays open — don't mark terminal in UI.
    if (active.source === 'ops' && status === 'succeeded') return
    updateJobStatus(active.taskId, status)
  }

  const opsLit = ops?.cycle_in_progress
    ? 'warn'
    : ops?.running && (ops.auto_probe_enabled || ops.auto_replenish_enabled)
      ? 'ok'
      : ops?.running
        ? 'accent'
        : 'danger'

  const counts = useMemo(() => {
    const c = { ops: 0, register: 0, batch: 0, action: 0, other: 0, running: 0 }
    for (const j of merged) {
      const k = (j.source || 'other') as keyof typeof c
      if (k in c && k !== 'running') c[k] += 1
      if (!j.status || ['running', 'claimed', 'pending'].includes(j.status)) c.running += 1
    }
    return c
  }, [merged])

  return (
    <div className="xy-page">
      <div className="xy-strip">
        <div>
          <div className="xy-k">{t('jobs.kicker')}</div>
          <h1 className="xy-h1">{t('jobs.title')}</h1>
          <p className="xy-sub">{t('jobs.subtitle')}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw className={cn('mr-1.5 h-3.5 w-3.5', loading && 'animate-spin')} />
            {t('common.refresh')}
          </Button>
          <Button variant="outline" size="sm" onClick={clearTerminal}>
            <Trash2 className="mr-1.5 h-3.5 w-3.5" />
            {t('jobs.clearTerminal')}
          </Button>
          <Link
            to="/accounts/chatgpt?mode=register"
            className="inline-flex h-8 items-center border-2 border-[var(--accent)] bg-[var(--accent)] px-3 text-[12px] font-bold text-[#04140f]"
          >
            {t('jobs.goRegister')}
          </Link>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <span className="xy-lamp xy-lamp-mag">
          {t('jobs.chipRunning')}: {counts.running}
        </span>
        {counts.ops > 0 && (
          <span className="xy-lamp xy-lamp-accent">
            {t('jobs.groupOps')}: {counts.ops}
          </span>
        )}
        {counts.register > 0 && (
          <span className="xy-lamp xy-lamp-ok">
            {t('jobs.groupRegister')}: {counts.register}
          </span>
        )}
        {counts.batch > 0 && (
          <span className="xy-lamp xy-lamp-warn">
            {t('jobs.groupCheck')}: {counts.batch}
          </span>
        )}
        {counts.action > 0 && (
          <span className="xy-lamp xy-lamp-cyan">
            {t('jobs.groupAction')}: {counts.action}
          </span>
        )}
      </div>

      <section className="xy-panel" aria-label="auto-ops status">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">{t('jobs.opsTitle')}</h2>
          <span
            className={cn(
              'xy-lamp',
              opsLit === 'ok' && 'xy-lamp-ok',
              opsLit === 'warn' && 'xy-lamp-warn',
              opsLit === 'accent' && 'xy-lamp-accent',
            )}
          >
            {ops?.cycle_in_progress
              ? t('jobs.opsRunningCycle')
              : ops?.running
                ? t('jobs.opsSchedulerOn')
                : t('jobs.opsSchedulerOff')}
          </span>
        </div>
        <div className="xy-panel-b space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            {ops?.auto_probe_enabled ? (
              <Button
                variant="outline"
                size="sm"
                disabled={ctrlBusy}
                onClick={() => runOpsControl('/auto-ops/stop-probe', { cancel_registers: false })}
              >
                <Square className="mr-1.5 h-3.5 w-3.5 text-[var(--danger)]" />
                {t('jobs.btnStopProbe')}
              </Button>
            ) : (
              <Button
                variant="outline"
                size="sm"
                disabled={ctrlBusy}
                onClick={() => runOpsControl('/auto-ops/start-probe')}
              >
                <Play className="mr-1.5 h-3.5 w-3.5 text-[var(--ok)]" />
                {t('jobs.btnStartProbe')}
              </Button>
            )}
            {ops?.auto_replenish_enabled || ops?.auto_register_enabled || ops?.register_task_active ? (
              <Button
                variant="outline"
                size="sm"
                disabled={ctrlBusy}
                onClick={() =>
                  runOpsControl('/auto-ops/stop-replenish', { cancel_running: true })
                }
              >
                <Square className="mr-1.5 h-3.5 w-3.5 text-[var(--danger)]" />
                {t('jobs.btnStopRegister')}
              </Button>
            ) : (
              <Button
                variant="outline"
                size="sm"
                disabled={ctrlBusy}
                onClick={() => runOpsControl('/auto-ops/start-replenish')}
              >
                <Play className="mr-1.5 h-3.5 w-3.5 text-[var(--ok)]" />
                {t('jobs.btnStartReplenish')}
              </Button>
            )}
            {ops?.register_task_active ? (
              <Button
                variant="outline"
                size="sm"
                disabled={ctrlBusy}
                onClick={() => runOpsControl('/auto-ops/stop-register-tasks')}
              >
                <OctagonX className="mr-1.5 h-3.5 w-3.5" />
                {t('jobs.btnCancelRegisterOnly')}
              </Button>
            ) : null}
            <Button
              variant="outline"
              size="sm"
              disabled={ctrlBusy}
              className="border-[var(--danger)] text-[var(--danger)]"
              onClick={() => runOpsControl('/auto-ops/stop-all', { cancel_registers: true })}
            >
              <OctagonX className="mr-1.5 h-3.5 w-3.5" />
              {t('jobs.btnStopAll')}
            </Button>
            {ctrlMsg ? (
              <span className="font-[family-name:var(--font-mono)] text-[11px] text-[var(--text-muted)]">
                {ctrlMsg}
              </span>
            ) : null}
          </div>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <div className="xy-kv border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2">
              <span>{t('jobs.opsProbe')}</span>
              <span className={ops?.auto_probe_enabled ? 'text-[var(--ok)]' : 'text-[var(--text-muted)]'}>
                {ops?.auto_probe_enabled ? t('common.enabled') : t('common.disabled')}
                {ops?.auto_ops_interval_seconds
                  ? ` · ${ops.auto_ops_interval_seconds}s`
                  : ''}
              </span>
            </div>
            <div className="xy-kv border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2">
              <span>{t('jobs.opsDelete')}</span>
              <span className={ops?.auto_delete_remote_enabled ? 'text-[var(--ok)]' : 'text-[var(--text-muted)]'}>
                {ops?.auto_delete_remote_enabled ? t('common.enabled') : t('common.disabled')}
              </span>
            </div>
            <div className="xy-kv border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2">
              <span>{t('jobs.opsReplenish')}</span>
              <span
                className={
                  ops?.auto_replenish_enabled || ops?.auto_register_enabled
                    ? 'text-[var(--ok)]'
                    : 'text-[var(--text-muted)]'
                }
              >
                {ops?.auto_replenish_enabled || ops?.auto_register_enabled
                  ? t('common.enabled')
                  : t('common.disabled')}
                {ops?.register_task_active ? ` · ${t('jobs.opsRegisterActive')}` : ''}
              </span>
            </div>
            <div className="xy-kv border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2">
              <span>{t('jobs.opsUpload')}</span>
              <span className={ops?.auto_upload_enabled ? 'text-[var(--ok)]' : 'text-[var(--text-muted)]'}>
                {ops?.auto_upload_enabled
                  ? `${t('common.enabled')} · ${ops.sync_target || '—'}`
                  : t('common.disabled')}
              </span>
            </div>
          </div>
          <div className="grid gap-2 sm:grid-cols-3 font-[family-name:var(--font-mono)] text-[11px] text-[var(--text-muted)]">
            <div>
              {t('jobs.opsLastCycle')}: {fmtTime(ops?.last_cycle_at)}
            </div>
            <div>
              {t('jobs.opsLastProbe')}: {ops?.last_probe_result || fmtTime(ops?.last_probe_at)}
            </div>
            <div>
              {t('jobs.opsLastReplenish')}: {fmtTime(ops?.last_replenish_at)}
            </div>
          </div>
          {(ops?.recent_logs || []).length > 0 ? (
            <div className="max-h-28 overflow-y-auto border border-[var(--border-soft)] bg-[var(--bg-input)] p-2 font-[family-name:var(--font-mono)] text-[11px]">
              {(ops?.recent_logs || []).slice(-12).map((line, i) => (
                <div
                  key={`${line.at}-${i}`}
                  className={cn(
                    'leading-4',
                    line.level === 'error' || line.level === 'warning'
                      ? 'text-[var(--danger)]'
                      : 'text-[var(--text-secondary)]',
                  )}
                >
                  <span className="text-[var(--text-muted)]">
                    [{fmtTime(line.at).split(' ').pop()}]
                  </span>{' '}
                  {line.message}
                </div>
              ))}
            </div>
          ) : (
            <div className="flex items-center gap-2 text-[12px] text-[var(--text-muted)]">
              <Radar className="h-3.5 w-3.5" />
              {t('jobs.opsNoRecent')}
            </div>
          )}
        </div>
      </section>

      {merged.length === 0 ? (
        <div className="empty-state-panel py-16">
          <Radio className="mx-auto mb-3 h-6 w-6 text-[var(--text-muted)]" />
          <div className="text-[14px] font-semibold text-[var(--text-primary)]">
            {t('jobs.emptyTitle')}
          </div>
          <p className="mx-auto mt-2 max-w-sm text-[13px]">{t('jobs.emptyDesc')}</p>
        </div>
      ) : (
        <div className="xy-jobs-layout">
          <aside className="xy-panel xy-jobs-queue">
            <div className="xy-panel-h shrink-0">
              <h2 className="xy-panel-t">{t('jobs.queue')}</h2>
              <span className="xy-lamp">{merged.length}</span>
            </div>
            <div className="xy-jobs-queue-list">
              {groups.map((group) => (
                <div key={group.source}>
                  <div className="sticky top-0 z-[1] border-b border-[var(--border-soft)] bg-[var(--bg-pane)] px-3 py-1.5 font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.1em] text-[var(--cyan)]">
                    {group.label}
                    <span className="ml-2 text-[var(--text-muted)]">{group.items.length}</span>
                  </div>
                  {group.items.map((job) => {
                    const on = active?.taskId === job.taskId
                    return (
                      <button
                        key={job.taskId}
                        type="button"
                        onClick={() => setActiveTaskId(job.taskId)}
                        className={cn(
                          'flex w-full flex-col gap-1 border-b border-[var(--border-soft)] px-3 py-3 text-left transition-colors',
                          on ? 'bg-[var(--accent-soft)]' : 'hover:bg-[var(--bg-hover)]',
                        )}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.08em] text-[var(--accent-strong)]">
                            {job.source}
                            {job.source === 'ops' ? ' · LIVE' : ''}
                          </span>
                          {job.source !== 'ops' ? (
                            <button
                              type="button"
                              className="text-[var(--text-muted)] hover:text-[var(--danger)]"
                              onClick={(e) => {
                                e.stopPropagation()
                                dismissJob(job.taskId)
                              }}
                              title={t('jobs.dismiss')}
                            >
                              <Trash2 className="h-3 w-3" />
                            </button>
                          ) : null}
                        </div>
                        <div className="line-clamp-2 text-[12px] font-semibold">{job.title}</div>
                        <div className="font-[family-name:var(--font-mono)] text-[10px] text-[var(--text-muted)]">
                          {job.source === 'ops'
                            ? t('jobs.opsSessionOpen')
                            : job.status
                              ? getTaskStatusText(job.status, language)
                              : t('taskStatus.running')}
                        </div>
                      </button>
                    )
                  })}
                </div>
              ))}
            </div>
          </aside>

          <section className="xy-panel xy-jobs-detail">
            <div className="xy-panel-h shrink-0">
              <div className="flex min-w-0 items-center gap-2">
                <Activity className="h-3.5 w-3.5 shrink-0 text-[var(--accent-strong)]" />
                <h2 className="xy-panel-t truncate">
                  {active ? active.title : t('jobs.selectTask')}
                </h2>
              </div>
              {active && (
                <span className="xy-lamp xy-lamp-accent font-[family-name:var(--font-mono)] text-[10px]">
                  {active.source === 'ops' ? 'SESSION' : active.taskId.slice(0, 8)}
                </span>
              )}
            </div>
            <div className="xy-panel-b xy-jobs-detail-body !p-3">
              {active ? (
                <TaskLogPanel taskId={active.taskId} onDone={handleDone} />
              ) : (
                <div className="empty-state-panel m-0 flex h-full items-center justify-center">
                  {t('jobs.selectHint')}
                </div>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  )
}

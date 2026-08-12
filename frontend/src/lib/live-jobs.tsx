import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

export type LiveJob = {
  taskId: string
  title: string
  source: 'register' | 'batch' | 'other'
  startedAt: number
  status?: string | null
  autoDownloadAgentIdentity?: boolean
}

type LiveJobsContextValue = {
  jobs: LiveJob[]
  activeTaskId: string | null
  setActiveTaskId: (taskId: string | null) => void
  trackJob: (
    job: Omit<LiveJob, 'startedAt'> & { startedAt?: number },
    opts?: { force?: boolean },
  ) => void
  updateJobStatus: (taskId: string, status: string) => void
  dismissJob: (taskId: string) => void
  clearTerminal: () => void
  isJobHidden: (taskId: string) => boolean
}

const STORAGE_KEY = 'xy-live-jobs-v1'
const HIDDEN_KEY = 'xy-live-jobs-hidden-v1'

const TERMINAL = new Set([
  'succeeded',
  'failed',
  'cancelled',
  'interrupted',
])

const LiveJobsContext = createContext<LiveJobsContextValue | null>(null)

function loadJobs(): LiveJob[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed)
      ? parsed.filter((item) =>
          ['register', 'batch', 'other'].includes(String(item?.source || '')),
        )
      : []
  } catch {
    return []
  }
}

function loadHidden(): Set<string> {
  try {
    const raw = localStorage.getItem(HIDDEN_KEY)
    if (!raw) return new Set()
    const parsed = JSON.parse(raw)
    return new Set(Array.isArray(parsed) ? parsed.map(String) : [])
  } catch {
    return new Set()
  }
}

function isTerminalStatus(status?: string | null): boolean {
  return !!status && TERMINAL.has(status)
}

export function LiveJobsProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<LiveJob[]>(() => loadJobs())
  const [hiddenIds, setHiddenIds] = useState<Set<string>>(() => loadHidden())
  const [activeTaskId, setActiveTaskId] = useState<string | null>(
    () => loadJobs().find((j) => !loadHidden().has(j.taskId))?.taskId || null,
  )

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(jobs.slice(0, 40)))
  }, [jobs])

  useEffect(() => {
    // Cap hidden list so it doesn't grow forever
    const arr = Array.from(hiddenIds).slice(-300)
    localStorage.setItem(HIDDEN_KEY, JSON.stringify(arr))
  }, [hiddenIds])

  const isJobHidden = useCallback(
    (taskId: string) => hiddenIds.has(taskId),
    [hiddenIds],
  )

  const trackJob = useCallback(
    (
      job: Omit<LiveJob, 'startedAt'> & { startedAt?: number },
      opts?: { force?: boolean },
    ) => {
      // Server poll re-injects finished tasks unless we respect the hide list.
      if (!opts?.force && hiddenIds.has(job.taskId)) {
        return
      }
      // force track (user started a new job) → unhide
      if (opts?.force && hiddenIds.has(job.taskId)) {
        setHiddenIds((prev) => {
          const next = new Set(prev)
          next.delete(job.taskId)
          return next
        })
      }
      setJobs((prev) => {
        const existing = prev.find((item) => item.taskId === job.taskId)
        const next = prev.filter((item) => item.taskId !== job.taskId)
        return [
          {
            ...existing,
            ...job,
            startedAt: job.startedAt || existing?.startedAt || Date.now(),
            status: job.status ?? existing?.status,
          },
          ...next,
        ].slice(0, 40)
      })
      setActiveTaskId((cur) => cur || job.taskId)
    },
    [hiddenIds],
  )

  const updateJobStatus = useCallback((taskId: string, status: string) => {
    setJobs((prev) =>
      prev.map((item) =>
        item.taskId === taskId ? { ...item, status } : item,
      ),
    )
  }, [])

  const dismissJob = useCallback((taskId: string) => {
    setHiddenIds((prev) => {
      const next = new Set(prev)
      next.add(taskId)
      return next
    })
    setJobs((prev) => prev.filter((item) => item.taskId !== taskId))
    setActiveTaskId((current) => (current === taskId ? null : current))
  }, [])

  const clearTerminal = useCallback(() => {
    setJobs((prev) => {
      const toHide: string[] = []
      const kept: LiveJob[] = []
      for (const item of prev) {
        if (!isTerminalStatus(item.status)) {
          kept.push(item)
        } else {
          toHide.push(item.taskId)
        }
      }
      if (toHide.length) {
        setHiddenIds((prevHidden) => {
          const next = new Set(prevHidden)
          for (const id of toHide) next.add(id)
          return next
        })
      }
      setActiveTaskId((cur) =>
        cur && toHide.includes(cur) ? kept[0]?.taskId || null : cur,
      )
      return kept
    })
  }, [])

  const value = useMemo(
    () => ({
      jobs,
      activeTaskId,
      setActiveTaskId,
      trackJob,
      updateJobStatus,
      dismissJob,
      clearTerminal,
      isJobHidden,
    }),
    [
      jobs,
      activeTaskId,
      trackJob,
      updateJobStatus,
      dismissJob,
      clearTerminal,
      isJobHidden,
    ],
  )

  return (
    <LiveJobsContext.Provider value={value}>{children}</LiveJobsContext.Provider>
  )
}

export function useLiveJobs() {
  const ctx = useContext(LiveJobsContext)
  if (!ctx) {
    throw new Error('useLiveJobs must be used within LiveJobsProvider')
  }
  return ctx
}

export function isTerminalJobStatus(status?: string | null): boolean {
  return isTerminalStatus(status)
}

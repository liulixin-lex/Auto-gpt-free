import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch } from '@/lib/utils'
import { translateAccountStatus } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { Button } from '@/components/ui/button'
import PromoBanner from '@/components/PromoBanner'
import { RefreshCw, Play, ListTree, Wrench } from 'lucide-react'

type PoolStats = {
  total?: number
  active?: number
  fault?: number
  by_platform?: Record<string, number>
  by_bucket?: Record<string, number>
}

const BUCKET_ORDER = ['registered', 'trial', 'subscribed', 'expired', 'invalid'] as const

const BUCKET_LIT: Record<string, string> = {
  registered: 'accent',
  trial: 'warn',
  subscribed: 'ok',
  expired: 'warn',
  invalid: 'danger',
}

export default function Dashboard() {
  const { t, language } = useI18n()
  const [stats, setStats] = useState<PoolStats | null>(null)
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      setStats(await apiFetch('/accounts/stats'))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const total = Number(stats?.total || 0)
  const active = Number(stats?.active ?? Math.max(0, total - Number(stats?.fault || 0)))
  const fault = Number(stats?.fault || 0)
  const buckets = stats?.by_bucket || {}
  const platformEntries = Object.entries(stats?.by_platform || {})
  const faultRate = total > 0 ? Math.round((fault / total) * 100) : 0

  const bucketRows = BUCKET_ORDER
    .map((key) => ({ key, count: Number(buckets[key] || 0) }))
    .filter((row) => row.count > 0 || ['registered', 'invalid'].includes(row.key))

  return (
    <div className="xy-page">
      <PromoBanner />

      <div className="xy-strip">
        <div>
          <div className="xy-k">概览</div>
          <h1 className="xy-h1">号池概览</h1>
          <p className="xy-sub">
            查看账号总量、可用数和失效数。注册与检测请到号池页操作。
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className={`mr-2 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
          刷新
        </Button>
      </div>

      <section className="xy-runbar">
        <div>
          <div className="xy-runbar-title">快捷入口</div>
          <div className="xy-runbar-desc">
            建议先在设置里配好邮箱和打码，再到号池注册或检测。
          </div>
          <div className="xy-runbar-actions">
            <Link
              to="/accounts/chatgpt?mode=register"
              className="inline-flex h-9 items-center gap-2 border-2 border-[var(--accent)] bg-[var(--accent)] px-3.5 text-[12px] font-bold text-[#04140f] hover:bg-[var(--accent-hover)]"
            >
              <Play className="h-3.5 w-3.5" />
              开始注册
            </Link>
            <Link
              to="/accounts/chatgpt?mode=library"
              className="inline-flex h-9 items-center gap-2 border-2 border-[var(--border)] bg-[var(--bg-pane)] px-3.5 text-[12px] font-semibold text-[var(--text-secondary)] hover:border-[var(--accent-edge)]"
            >
              <ListTree className="h-3.5 w-3.5" />
              打开号池
            </Link>
            <Link
              to="/settings?tab=mailbox"
              className="inline-flex h-9 items-center gap-2 border-2 border-[var(--border)] bg-[var(--bg-pane)] px-3.5 text-[12px] font-semibold text-[var(--text-secondary)] hover:border-[var(--accent-edge)]"
            >
              <Wrench className="h-3.5 w-3.5" />
              邮箱 / 打码
            </Link>
          </div>
        </div>
        <div className="xy-runbar-side">
          <div className="xy-kv">
            <span>状态</span>
            <span>{loading ? '读取中' : '就绪'}</span>
          </div>
          <div className="xy-kv">
            <span>平台数</span>
            <span>{platformEntries.length || '—'}</span>
          </div>
          <div className="xy-kv">
            <span>失效占比</span>
            <span>{stats ? `${faultRate}%` : '—'}</span>
          </div>
        </div>
      </section>

      <section className="xy-panel" aria-label="pool inventory">
        <div className="xy-panel-h">
          <h2 className="xy-panel-t">库存统计</h2>
          <span className="xy-lamp xy-lamp-accent">
            {stats ? `共 ${total}` : '…'}
          </span>
        </div>
        <div className="xy-panel-b space-y-5">
          <div className="xy-meters !grid-cols-3">
            <div className="xy-meter" data-lit="accent">
              <div className="xy-meter-l">总量</div>
              <div className="xy-meter-v">{stats ? total : '—'}</div>
              <div className="xy-meter-f">在库账号</div>
            </div>
            <div className="xy-meter" data-lit="ok">
              <div className="xy-meter-l">可用</div>
              <div className="xy-meter-v">{stats ? active : '—'}</div>
              <div className="xy-meter-f">非失效账号</div>
            </div>
            <div className="xy-meter" data-lit="danger">
              <div className="xy-meter-l">失效</div>
              <div className="xy-meter-v">{stats ? fault : '—'}</div>
              <div className="xy-meter-f">失效 + 过期</div>
            </div>
          </div>

          <div className="xy-grid-2">
            <div className="space-y-2">
              <div className="font-[family-name:var(--font-mono)] text-[11px] text-[var(--text-muted)]">
                状态分布
              </div>
              {stats && bucketRows.length > 0 ? (
                bucketRows.map(({ key, count }) => {
                  const ratio = total > 0 ? Math.round((count / total) * 100) : 0
                  return (
                    <div key={key} className="border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2">
                      <div className="mb-1.5 flex items-center justify-between gap-2 text-[12px]">
                        <span className="flex items-center gap-2">
                          <span
                            className="inline-block h-2 w-2 rounded-full"
                            style={{
                              background:
                                BUCKET_LIT[key] === 'ok'
                                  ? 'var(--ok)'
                                  : BUCKET_LIT[key] === 'danger'
                                    ? 'var(--danger)'
                                    : BUCKET_LIT[key] === 'warn'
                                      ? 'var(--warn)'
                                      : 'var(--accent)',
                            }}
                          />
                          <span className="font-semibold">
                            {translateAccountStatus(key, language)}
                          </span>
                        </span>
                        <span className="font-[family-name:var(--font-mono)] tabular-nums text-[var(--text-muted)]">
                          {count} · {ratio}%
                        </span>
                      </div>
                      <div className="progress-track">
                        <div className="progress-fill" style={{ width: `${ratio}%` }} />
                      </div>
                    </div>
                  )
                })
              ) : (
                <div className="empty-state-panel py-6 text-[12px]">
                  {stats ? t('dashboard.noData') : t('dashboard.loadingStats')}
                </div>
              )}
            </div>

            <div className="space-y-2">
              <div className="font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.1em] text-[var(--text-muted)]">
                平台占比
              </div>
              {platformEntries.length > 0 ? (
                platformEntries.map(([platform, count]) => {
                  const countValue = Number(count) || 0
                  const ratio = total > 0 ? Math.round((countValue / total) * 100) : 0
                  return (
                    <div key={platform} className="border border-[var(--border)] bg-[var(--bg-input)] px-3 py-2">
                      <div className="mb-1.5 flex items-center justify-between text-[12px]">
                        <span className="font-[family-name:var(--font-mono)] uppercase tracking-wide">
                          {platform}
                        </span>
                        <span className="font-[family-name:var(--font-mono)] tabular-nums text-[var(--text-muted)]">
                          {countValue} · {ratio}%
                        </span>
                      </div>
                      <div className="progress-track">
                        <div className="progress-fill" style={{ width: `${ratio}%` }} />
                      </div>
                    </div>
                  )
                })
              ) : (
                <div className="empty-state-panel py-6 text-[12px]">
                  {stats ? t('dashboard.noPlatformData') : t('dashboard.loadingStats')}
                </div>
              )}
            </div>
          </div>
        </div>
      </section>
    </div>
  )
}

import { useCallback, useEffect, useState } from 'react'
import ProviderCards from '@/components/settings/ProviderCards'
import { getConfigOptions } from '@/lib/app-data'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'
import { useI18n } from '@/lib/i18n-context'

export default function Settings({
  providerType = 'mailbox',
}: {
  providerType?: 'mailbox' | 'captcha'
}) {
  const { t } = useI18n()
  const [catalog, setCatalog] = useState<ProviderOption[]>([])
  const [settings, setSettings] = useState<ProviderSetting[]>([])
  const [error, setError] = useState('')

  const loadProviders = useCallback(async () => {
    try {
      const options = await getConfigOptions()
      const isCaptcha = providerType === 'captcha'
      setCatalog(
        isCaptcha
          ? options.captcha_providers || []
          : options.mailbox_providers || [],
      )
      setSettings(
        isCaptcha
          ? options.captcha_settings || []
          : options.mailbox_settings || [],
      )
      setError('')
    } catch {
      setCatalog([])
      setSettings([])
      setError(t('register.providerMetadataError'))
    }
  }, [providerType, t])

  useEffect(() => {
    void loadProviders()
  }, [loadProviders])

  const isCaptcha = providerType === 'captcha'
  const defaultName =
    settings.find((s) => s.is_default)?.display_name ||
    settings.find((s) => s.is_default)?.provider_key ||
    '—'

  return (
    <div className="space-y-3">
      {error && (
        <div className="border border-[var(--danger)] bg-[var(--danger-soft)] px-3 py-2 text-[12px] text-[var(--danger)]">
          {error}
        </div>
      )}

      <div className="xy-meters !grid-cols-3">
        <div className="xy-meter" data-lit="accent">
          <div className="xy-meter-l">catalog</div>
          <div className="xy-meter-v text-[22px]">{catalog.length}</div>
          <div className="xy-meter-f">可选驱动</div>
        </div>
        <div className="xy-meter" data-lit="ok">
          <div className="xy-meter-l">armed</div>
          <div className="xy-meter-v text-[22px]">{settings.length}</div>
          <div className="xy-meter-f">已挂载</div>
        </div>
        <div className="xy-meter" data-lit="warn">
          <div className="xy-meter-l">primary</div>
          <div className="xy-meter-v text-[14px] leading-tight">{defaultName}</div>
          <div className="xy-meter-f">默认路由</div>
        </div>
      </div>

      <div className="border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-3 py-2 text-[12px] text-[var(--text-secondary)]">
        {isCaptcha
          ? '协议模式按启用顺序自动选远程打码；浏览器模式走默认 provider。行内可测连通。'
          : '仅当注册身份为系统邮箱时消耗此处资源。先启用并设默认，再回 RUN 舱起任务。'}
      </div>

      <ProviderCards
        providerType={providerType}
        catalog={catalog}
        settings={settings}
        onReload={loadProviders}
      />
    </div>
  )
}

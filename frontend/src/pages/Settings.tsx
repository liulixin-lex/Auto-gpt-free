import { useCallback, useEffect, useState } from 'react'
import ProviderCards from '@/components/settings/ProviderCards'
import { getConfigOptions } from '@/lib/app-data'
import type { ProviderOption, ProviderSetting } from '@/lib/config-options'
import { useI18n } from '@/lib/i18n-context'
import { localizeEventMessage } from '@/lib/i18n'

export default function Settings({
  providerType = 'mailbox',
}: {
  providerType?: 'mailbox' | 'captcha'
}) {
  const { t, language } = useI18n()
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
  const defaultNameRaw =
    settings.find((s) => s.is_default)?.display_name ||
    settings.find((s) => s.is_default)?.provider_key ||
    '?'
  const defaultName = localizeEventMessage(defaultNameRaw, language)

  return (
    <div className="space-y-3">
      {error && (
        <div className="border border-[var(--danger)] bg-[var(--danger-soft)] px-3 py-2 text-[12px] text-[var(--danger)]">
          {error}
        </div>
      )}

      <div className="xy-meters !grid-cols-3">
        <div className="xy-meter" data-lit="accent">
          <div className="xy-meter-l">{t('providers.catalog')}</div>
          <div className="xy-meter-v text-[22px]">{catalog.length}</div>
          <div className="xy-meter-f">{t('providers.catalog')}</div>
        </div>
        <div className="xy-meter" data-lit="ok">
          <div className="xy-meter-l">{t('providers.armed')}</div>
          <div className="xy-meter-v text-[22px]">{settings.length}</div>
          <div className="xy-meter-f">{t('providers.armed')}</div>
        </div>
        <div className="xy-meter" data-lit="warn">
          <div className="xy-meter-l">{t('providers.primary')}</div>
          <div className="xy-meter-v text-[14px] leading-tight">{defaultName}</div>
          <div className="xy-meter-f">{t('providers.primary')}</div>
        </div>
      </div>

      <div className="border border-[var(--accent-edge)] bg-[var(--accent-soft)] px-3 py-2 text-[12px] text-[var(--text-secondary)]">
        {isCaptcha
          ? t('providers.captchaUsageShort')
          : t('providers.mailboxUsageShort')}
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

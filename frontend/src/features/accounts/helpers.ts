import {
  DEFAULT_LANGUAGE,
  localizeEventMessage,
  translate,
  translateAccountStatus,
  type Language,
  type TranslationKey,
} from '@/lib/i18n'

export const STATUS_VARIANT: Record<string, any> = {
  registered: 'default',
  trial: 'success',
  subscribed: 'success',
  expired: 'warning',
  invalid: 'danger',
  free: 'secondary',
  eligible: 'secondary',
  valid: 'success',
  unknown: 'secondary',
}

export function getAccountOverview(acc: any) {
  return acc?.overview || {}
}

export function getDisplaySummary(acc: any) {
  return acc?.display_summary && typeof acc.display_summary === 'object'
    ? acc.display_summary
    : {}
}

export function getVerificationMailbox(acc: any) {
  const providerResources = Array.isArray(acc?.provider_resources)
    ? acc.provider_resources
    : []
  const normalized = providerResources.find(
    (item: any) => item?.resource_type === 'mailbox',
  )
  if (normalized) {
    return {
      provider: normalized.provider_name,
      email: normalized.handle || normalized.display_name,
      account_id: normalized.resource_identifier,
    }
  }
  return null
}

export function getLifecycleStatus(acc: any) {
  return (
    getDisplaySummary(acc)?.status?.lifecycle ||
    acc?.lifecycle_status ||
    'registered'
  )
}

export function getDisplayStatus(acc: any) {
  return (
    getDisplaySummary(acc)?.status?.display ||
    acc?.display_status ||
    acc?.plan_state ||
    getLifecycleStatus(acc)
  )
}

export function getPlanState(acc: any) {
  return (
    getDisplaySummary(acc)?.status?.plan_state ||
    acc?.plan_state ||
    acc?.overview?.plan_state ||
    'unknown'
  )
}

export function getValidityStatus(acc: any) {
  return (
    getDisplaySummary(acc)?.status?.validity ||
    acc?.validity_status ||
    acc?.overview?.validity_status ||
    'unknown'
  )
}

export function getCompactStatusMeta(acc: any, language: Language = DEFAULT_LANGUAGE) {
  const summary = getDisplaySummary(acc)
  const primaryMetrics = Array.isArray(summary?.primary_metrics)
    ? summary.primary_metrics
    : []
  if (primaryMetrics.length > 0) {
    return primaryMetrics
      .slice(0, 2)
      .map((item: any) => {
        const sub = item?.sub ? ` · ${localizeEventMessage(String(item.sub), language)}` : ''
        const label = item?.label ? localizeEventMessage(String(item.label), language) : ''
        return `${label}:${item?.value || '-'}${sub}`
      })
      .join(' / ')
  }
  const overview = getAccountOverview(acc)
  const parts = [
    `${translate('accounts.lifecycle', language)}:${translateAccountStatus(getLifecycleStatus(acc), language)}`,
    `${translate('accounts.plan', language)}:${translateAccountStatus(getPlanState(acc), language)}`,
    `${translate('accounts.validity', language)}:${translateAccountStatus(getValidityStatus(acc), language)}`,
  ]
  const remainingCredits = overview?.remaining_credits
  const usageTotal = overview?.usage_total
  if (remainingCredits || usageTotal) {
    parts.push(
      `${translate('accounts.credits', language)}:${remainingCredits || '-'} / ${translate('accounts.used', language)}:${usageTotal || '-'}`,
    )
  }
  return parts.join(' / ')
}

export function getPrimaryMetrics(acc: any) {
  const metrics = getDisplaySummary(acc)?.primary_metrics
  return Array.isArray(metrics) ? metrics : []
}

export function getSecondaryMetrics(acc: any) {
  const metrics = getDisplaySummary(acc)?.secondary_metrics
  return Array.isArray(metrics) ? metrics : []
}

export function getDisplayWarnings(acc: any) {
  const warnings = getDisplaySummary(acc)?.warnings
  return Array.isArray(warnings) ? warnings : []
}

export function getDisplayBadges(acc: any) {
  const badges = getDisplaySummary(acc)?.badges
  return Array.isArray(badges) ? badges : []
}

export function getDisplaySections(acc: any) {
  const sections = getDisplaySummary(acc)?.sections
  return Array.isArray(sections) ? sections : []
}

export function getProviderAccounts(acc: any) {
  return Array.isArray(acc?.provider_accounts) ? acc.provider_accounts : []
}

export function getCredentials(acc: any) {
  return Array.isArray(acc?.credentials) ? acc.credentials : []
}

export function getCashierUrl(acc: any) {
  const overview = getAccountOverview(acc)
  return overview?.cashier_url || acc?.cashier_url || ''
}

export function getPrimaryToken(acc: any) {
  if (acc?.primary_token) return acc.primary_token
  const credential = getCredentials(acc).find(
    (item: any) =>
      item?.scope === 'platform' &&
      item?.credential_type === 'token' &&
      item?.value,
  )
  return credential?.value || ''
}

export function formatResultValue(value: any, language: Language = DEFAULT_LANGUAGE) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') {
    return translate(value ? 'common.yes' : 'common.no', language)
  }
  return String(value)
}

export function emailApiLine(email: string) {
  return `${email} https://hsxhome.com/api/find/openai?email=${email}&t=fzKIywnF4KEGGB_i`
}

export function copyText(text: string) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text)
  } else {
    const el = document.createElement('textarea')
    el.value = text
    document.body.appendChild(el)
    el.select()
    document.execCommand('copy')
    document.body.removeChild(el)
  }
}

/** Backend-supported export formats only (see api/accounts.py). */
export const ACCOUNT_EXPORT_FORMATS = [
  {
    key: 'json',
    labelKey: 'accounts.exportFormat.json.label',
    hintKey: 'accounts.exportFormat.json.hint',
  },
  {
    key: 'sub2api',
    labelKey: 'accounts.exportFormat.sub2api.label',
    hintKey: 'accounts.exportFormat.sub2api.hint',
  },
  {
    key: 'sub2api-agent-identity',
    labelKey: 'accounts.exportFormat.agentIdentity.label',
    hintKey: 'accounts.exportFormat.agentIdentity.hint',
  },
  {
    key: 'cpa',
    labelKey: 'accounts.exportFormat.cpa.label',
    hintKey: 'accounts.exportFormat.cpa.hint',
  },
] as const

export function getAccountExportFormats(language: Language = DEFAULT_LANGUAGE) {
  return ACCOUNT_EXPORT_FORMATS.map((option) => ({
    key: option.key,
    label: translate(option.labelKey as TranslationKey, language),
    hint: translate(option.hintKey as TranslationKey, language),
  }))
}

export type AccountExportFormat = (typeof ACCOUNT_EXPORT_FORMATS)[number]['key']

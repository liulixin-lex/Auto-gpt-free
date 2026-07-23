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

export const platformActionsCache = new Map<string, any[]>()
export const platformActionsPromiseCache = new Map<string, Promise<any[]>>()

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

export function getCompactStatusMeta(acc: any) {
  const summary = getDisplaySummary(acc)
  const primaryMetrics = Array.isArray(summary?.primary_metrics)
    ? summary.primary_metrics
    : []
  if (primaryMetrics.length > 0) {
    return primaryMetrics
      .slice(0, 2)
      .map((item: any) => {
        const sub = item?.sub ? ` · ${item.sub}` : ''
        return `${item?.label || ''}:${item?.value || '-'}${sub}`
      })
      .join(' / ')
  }
  const overview = getAccountOverview(acc)
  const parts = [
    `生命周期:${getLifecycleStatus(acc)}`,
    `套餐:${getPlanState(acc)}`,
    `有效:${getValidityStatus(acc)}`,
  ]
  const remainingCredits = overview?.remaining_credits
  const usageTotal = overview?.usage_total
  if (remainingCredits || usageTotal) {
    parts.push(`额度:${remainingCredits || '-'} / 已用:${usageTotal || '-'}`)
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

export function formatResultValue(value: any) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}

export function buildActionParamDraft(action: any, acc: any) {
  const params = Array.isArray(action?.params) ? action.params : []
  const emailPrefix = String(acc?.email || '').split('@')[0] || 'Development'
  const draft: Record<string, string> = {}
  params.forEach((param: any) => {
    if (action?.id === 'create_api_key' && param?.key === 'name') {
      draft[param.key] = `${emailPrefix}Development`
      return
    }
    if (Array.isArray(param?.options) && param.options.length > 0) {
      draft[param?.key || ''] = String(param.options[0] ?? '')
      return
    }
    draft[param?.key || ''] = ''
  })
  return draft
}

export async function loadPlatformActions(
  platform: string,
  options?: { force?: boolean },
  apiFetch?: (path: string) => Promise<any>,
) {
  const key = String(platform || '').trim()
  if (!key || !apiFetch) return []
  const force = Boolean(options?.force)
  if (!force && platformActionsCache.has(key)) {
    return platformActionsCache.get(key) || []
  }
  if (!force && platformActionsPromiseCache.has(key)) {
    return platformActionsPromiseCache.get(key) || []
  }
  const pending = apiFetch(`/actions/${key}`)
    .then((data) => {
      const actions = Array.isArray(data?.actions) ? data.actions : []
      platformActionsCache.set(key, actions)
      platformActionsPromiseCache.delete(key)
      return actions
    })
    .catch((error) => {
      platformActionsPromiseCache.delete(key)
      throw error
    })
  platformActionsPromiseCache.set(key, pending)
  return pending
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
    label: 'JSON 凭据包',
    hint: 'email / token / session 原始包',
  },
  {
    key: 'sub2api',
    label: 'Sub2API OAuth',
    hint: '上游 free 机制 · 不调 agent/register',
  },
  {
    key: 'sub2api-agent-identity',
    label: 'Sub2API Agent Identity',
    hint: '优先私钥身份；Registry 关闭则自动 OAuth',
  },
  {
    key: 'cpa',
    label: 'CPA Token',
    hint: 'CPA 管理台 token JSON',
  },
] as const

export type AccountExportFormat = (typeof ACCOUNT_EXPORT_FORMATS)[number]['key']

import { translate, type Language } from '@/lib/i18n'
import { apiFetch } from '@/lib/utils'

export const TASK_STATUS_VARIANTS: Record<string, any> = {
  pending: 'secondary',
  claimed: 'secondary',
  running: 'default',
  succeeded: 'success',
  partial: 'warning',
  failed: 'danger',
  interrupted: 'warning',
  cancel_requested: 'warning',
  cancelled: 'warning',
  timed_out: 'danger',
}

export const TERMINAL_TASK_STATUSES = new Set([
  'succeeded',
  'partial',
  'failed',
  'interrupted',
  'cancelled',
  'timed_out',
])

export const ACTIVE_CANCELLABLE_TASK_STATUSES = new Set([
  'pending',
  'claimed',
  'running',
])

export function isTerminalTaskStatus(status: string) {
  return TERMINAL_TASK_STATUSES.has(status)
}

export function isCancellableTaskStatus(status: string) {
  return ACTIVE_CANCELLABLE_TASK_STATUSES.has(status)
}

/** Request a cooperative stop and return the server's authoritative task row. */
export function cancelTask(taskId: string) {
  return apiFetch(`/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: 'POST',
  })
}

export function getTaskStatusText(status: string, language?: Language) {
  switch (status) {
    case 'succeeded':
      return translate('taskStatus.succeeded', language)
    case 'partial':
      return translate('taskStatus.partial', language)
    case 'failed':
      return translate('taskStatus.failed', language)
    case 'interrupted':
      return translate('taskStatus.interrupted', language)
    case 'cancelled':
      return translate('taskStatus.cancelled', language)
    case 'timed_out':
      return translate('taskStatus.timed_out', language)
    case 'cancel_requested':
      return translate('taskStatus.cancel_requested', language)
    case 'running':
      return translate('taskStatus.running', language)
    case 'claimed':
      return translate('taskStatus.claimed', language)
    case 'pending':
      return translate('taskStatus.pending', language)
    default:
      return status
  }
}

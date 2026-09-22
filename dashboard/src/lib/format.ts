import type { ColorSet } from '@/lib/styles'
import type { Status, TaskSummary } from '@/api'

/** Past this, an in_progress task counts as stuck. */
export const STALE_AFTER_S = 300

export const STATUS_COLOR: Record<Status, ColorSet> = {
  pending: 'neutral',
  in_progress: 'blue',
  blocked: 'violet',
  done: 'green',
  failed: 'destructive',
  cancelled: 'neutral',
}

export function ago(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} h ago`
  return `${Math.floor(seconds / 86_400)} d ago`
}

export function at(iso: string): string {
  return new Date(iso).toLocaleString('ru-RU')
}

/**
 * A task that has most likely been abandoned. With a lease this is a fact —
 * the term ran out; without one it stays a guess from the last update time.
 */
export function isStale(task: TaskSummary): boolean {
  if (task.status !== 'in_progress') return false
  if (task.lease_expires) return new Date(task.lease_expires).getTime() < Date.now()
  return (Date.now() - new Date(task.updated_at).getTime()) / 1000 > STALE_AFTER_S
}

/** How long until the lease expires, in words. */
export function leaseLeft(task: TaskSummary): string | null {
  if (!task.lease_expires || task.status !== 'in_progress') return null
  const seconds = (new Date(task.lease_expires).getTime() - Date.now()) / 1000
  if (seconds <= 0) return 'expired'
  if (seconds < 60) return `${Math.ceil(seconds)}s`
  return `${Math.ceil(seconds / 60)} min`
}

export const EVENT_LABEL: Record<string, string> = {
  created: 'created',
  claimed: 'claimed',
  status: 'status change',
  retry: 'sent for a retry',
  reaped: 'returned to the queue',
  dead_letter: 'attempts exhausted',
  blocked: 'blocked',
  unblocked: 'unblocked',
}

export function title(task: Pick<TaskSummary, 'task'>): string {
  const payload = task.task
  const named = payload.title ?? payload.name
  if (typeof named === 'string' && named.trim()) return named
  return JSON.stringify(payload)
}

export function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

import type { ColorSet } from '@/lib/styles'
import type { Status, TaskSummary } from '@/api'

/** Порог, после которого in_progress считается залипшей. */
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
  if (seconds < 60) return 'только что'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)} ч назад`
  return `${Math.floor(seconds / 86_400)} дн назад`
}

export function at(iso: string): string {
  return new Date(iso).toLocaleString('ru-RU')
}

export function isStale(task: TaskSummary): boolean {
  if (task.status !== 'in_progress') return false
  return (Date.now() - new Date(task.updated_at).getTime()) / 1000 > STALE_AFTER_S
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

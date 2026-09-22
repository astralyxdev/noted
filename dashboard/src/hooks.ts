import { useCallback, useEffect, useRef, useState } from 'react'

import { api, NONE, STATUSES, type Filters, type Stats, type Status, type TaskSummary } from '@/api'

const DEFAULT_LIMIT = 100

const EMPTY_STATS: Stats = {
  pending: 0,
  in_progress: 0,
  blocked: 0,
  done: 0,
  failed: 0,
  cancelled: 0,
  total: 0,
}

/** Состояние страницы живёт в URL: ссылкой на отфильтрованный вид можно поделиться. */
function readFilters(): Filters {
  const query = new URLSearchParams(window.location.search)
  const known = new Set<string>(STATUSES)
  const limit = Number(query.get('limit'))
  const stale = Number(query.get('stale'))
  return {
    status: query.getAll('status').filter((s) => known.has(s)) as Status[],
    project: query.get('project'),
    assignee: query.get('assignee'),
    stale: Number.isFinite(stale) && stale > 0 ? stale : null,
    limit: Number.isFinite(limit) && limit > 0 ? Math.min(limit, 500) : DEFAULT_LIMIT,
  }
}

function writeFilters(filters: Filters, task: number | null) {
  const query = new URLSearchParams()
  filters.status.forEach((s) => query.append('status', s))
  if (filters.project) query.set('project', filters.project)
  if (filters.assignee) query.set('assignee', filters.assignee)
  if (filters.stale) query.set('stale', String(filters.stale))
  if (filters.limit !== DEFAULT_LIMIT) query.set('limit', String(filters.limit))
  if (task) query.set('task', String(task))
  const search = query.toString()
  window.history.replaceState(null, '', search ? `/?${search}` : '/')
}

export function useFilters() {
  const [filters, setFilters] = useState<Filters>(readFilters)
  const patch = useCallback(
    (next: Partial<Filters>) => setFilters((current) => ({ ...current, ...next })),
    [],
  )
  const toggleStatus = useCallback(
    (status: Status) =>
      setFilters((current) => ({
        ...current,
        status: current.status.includes(status)
          ? current.status.filter((s) => s !== status)
          : [...current.status, status],
      })),
    [],
  )
  return { filters, patch, toggleStatus, setFilters }
}

export function useUrlSync(filters: Filters, openTask: number | null) {
  useEffect(() => writeFilters(filters, openTask), [filters, openTask])
}

export function openTaskFromUrl(): number | null {
  const id = Number(new URLSearchParams(window.location.search).get('task'))
  return Number.isFinite(id) && id > 0 ? id : null
}

export function useDashboard(filters: Filters) {
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [stats, setStats] = useState<Stats>(EMPTY_STATS)
  const [projects, setProjects] = useState<string[]>([])
  const [assignees, setAssignees] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(true)
  const inFlight = useRef(false)

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const [list, overview] = await Promise.all([
        api.list(filters),
        api.overview(filters.project === NONE ? NONE : filters.project),
      ])
      setTasks(list)
      setStats(overview.stats)
      setProjects(overview.projects)
      setAssignees(overview.assignees)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      inFlight.current = false
      setPending(false)
    }
  }, [filters])

  useEffect(() => {
    setPending(true)
    void refresh()
  }, [refresh])

  return { tasks, stats, projects, assignees, error, pending, refresh }
}

export type LiveState = 'connecting' | 'live' | 'down'

/** Сервер сам сообщает об изменениях. Опроса по таймеру нет. */
export function useLive(onChange: () => void): LiveState {
  const [state, setState] = useState<LiveState>('connecting')
  const handler = useRef(onChange)
  handler.current = onChange

  useEffect(() => {
    // TS сужает `'EventSource' in window` до всегда-истины, поэтому проверяем типом.
    if (typeof EventSource === 'undefined') {
      const timer = setInterval(() => handler.current(), 10_000)
      return () => clearInterval(timer)
    }
    const source = new EventSource('/events')
    source.addEventListener('tasks', () => handler.current())
    source.onopen = () => setState('live')
    source.onerror = () => setState('down')
    const onVisible = () => {
      if (!document.hidden) handler.current()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      source.close()
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  return state
}

/** Относительное время должно стареть само, без перезагрузки страницы. */
export function useTicker(everyMs = 30_000) {
  const [, tick] = useState(0)
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), everyMs)
    return () => window.clearInterval(timer)
  }, [everyMs])
}

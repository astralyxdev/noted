import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, api, NONE, STATUSES, type Filters, type Stats, type Status, type TaskSummary } from '@/api'

const DEFAULT_LIMIT = 50

/** How many rows to pull per scroll. */
export const PAGE = 50

/**
 * Ceiling of the window re-read on every server event. Past it the tail stays
 * as it was loaded: those are old tasks, they barely change, and pulling
 * thousands of rows on every change would buy nothing.
 */
const LIVE_WINDOW_CAP = 500

const EMPTY_STATS: Stats = {
  pending: 0,
  in_progress: 0,
  blocked: 0,
  done: 0,
  failed: 0,
  cancelled: 0,
  total: 0,
}

/** Page state lives in the URL, so a filtered view can be shared as a link. */
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
  const [locked, setLocked] = useState(false)
  const [pending, setPending] = useState(true)
  const [more, setMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const inFlight = useRef(false)
  // A refresh that arrived while the list was busy is not dropped: dropping it
  // loses whatever the live event was announcing. It is run once afterwards.
  const missed = useRef(false)
  const loaded = useRef(PAGE)
  const shown = useRef<TaskSummary[]>([])
  shown.current = tasks
  const again = useRef<() => void>(() => {})

  const refresh = useCallback(async () => {
    if (inFlight.current) {
      missed.current = true
      return
    }
    inFlight.current = true
    try {
      // `filters.limit` is in the URL, so it has to mean something: it is the
      // page size, and the window only ever grows past it by paging.
      const want = Math.min(Math.max(loaded.current, filters.limit || PAGE), LIVE_WINDOW_CAP)
      const [list, overview] = await Promise.all([
        api.list(filters, { limit: want }),
        api.overview(filters.project === NONE ? NONE : filters.project),
      ])
      // The tail beyond the window is kept: re-reading it on every event is costly.
      setTasks((prev) => (prev.length > want ? [...list, ...prev.slice(want)] : list))
      setMore((prev) => (shown.current.length > want ? prev : list.length === want))
      setStats(overview.stats)
      setProjects(overview.projects)
      setAssignees(overview.assignees)
      setError(null)
      setLocked(false)
    } catch (cause) {
      if (cause instanceof ApiError && cause.outcome === 'unauthorized') setLocked(true)
      else setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      inFlight.current = false
      setPending(false)
      if (missed.current) {
        missed.current = false
        again.current()
      }
    }
  }, [filters])

  again.current = refresh

  const loadMore = useCallback(async () => {
    const last = shown.current[shown.current.length - 1]
    if (!last || loadingMore || inFlight.current) return
    // Held for the same reason refresh holds it: a refresh landing mid-page
    // re-reads the top of the list while this cursor walks the bottom, and a
    // task created in between falls into the gap and is never shown.
    inFlight.current = true
    setLoadingMore(true)
    const page = filters.limit || PAGE
    try {
      const next = await api.list(filters, { limit: page, beforeId: last.id })
      if (next.length > 0) {
        setTasks((prev) => [...prev, ...next])
        loaded.current += next.length
      }
      setMore(next.length === page)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLoadingMore(false)
      inFlight.current = false
      if (missed.current) {
        missed.current = false
        again.current()
      }
    }
  }, [filters, loadingMore])

  useEffect(() => {
    // Filters changed, so the list starts over: otherwise the tail of the
    // previous result would stick to the new one.
    loaded.current = filters.limit || PAGE
    setTasks([])
    setMore(false)
    setPending(true)
    void refresh()
  }, [refresh])

  return { tasks, stats, projects, assignees, error, locked, pending, more, loadingMore, loadMore, refresh }
}

/** Pulls the next page once the bottom of the list approaches the screen. */
export function useNearBottom(enabled: boolean, onReach: () => void) {
  const anchor = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const node = anchor.current
    if (!enabled || !node || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) onReach()
      },
      { rootMargin: '300px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [enabled, onReach])

  return anchor
}

export type LiveState = 'connecting' | 'live' | 'down'

/** The server announces changes itself. There is no timer-based polling.
 *
 * `enabled` is what stops the stream being opened before the user is signed
 * in. `/events` answers 401 then, and a non-2xx first response is fatal to an
 * EventSource — the browser does not retry it — so the stream stayed dead for
 * the rest of the session and live updates never arrived, with only a reload
 * to fix it.
 */
export function useLive(onChange: () => void, enabled = true): LiveState {
  const [state, setState] = useState<LiveState>('connecting')
  const handler = useRef(onChange)
  handler.current = onChange

  useEffect(() => {
    if (!enabled) {
      setState('connecting')
      return
    }
    // TS narrows `'EventSource' in window` to always-true, so check the type.
    if (typeof EventSource === 'undefined') {
      const timer = setInterval(() => handler.current(), 10_000)
      return () => clearInterval(timer)
    }
    let source: EventSource
    let retry: ReturnType<typeof setTimeout> | undefined
    let stopped = false

    const open = () => {
      source = new EventSource('/events')
      // An event carries the whole transition, but the dashboard only needs the
      // fact of a change: it re-reads its window instead of rebuilding state.
      source.addEventListener('task', () => handler.current())
      source.onopen = () => setState('live')
      source.onerror = () => {
        setState('down')
        // CLOSED means the browser has given up for good — a refused first
        // response, typically. Reopening is the only way back.
        if (!stopped && source.readyState === EventSource.CLOSED) {
          retry = setTimeout(open, 3000)
        }
      }
    }
    open()

    const onVisible = () => {
      if (!document.hidden) handler.current()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      stopped = true
      if (retry) clearTimeout(retry)
      source.close()
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [enabled])

  return state
}

/** Relative time has to age on its own, without a page reload. */
export function useTicker(everyMs = 30_000) {
  const [, tick] = useState(0)
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), everyMs)
    return () => window.clearInterval(timer)
  }, [everyMs])
}

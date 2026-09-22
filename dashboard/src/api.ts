/** Тонкий клиент к ядру. Формы ответа заданы конвертом из SPEC.md. */

export const STATUSES = ['pending', 'in_progress', 'blocked', 'done', 'failed', 'cancelled'] as const

export type Status = (typeof STATUSES)[number]

export const TERMINAL: readonly Status[] = ['done', 'failed', 'cancelled']

export const OPEN: readonly Status[] = STATUSES.filter((s) => !TERMINAL.includes(s))

export type TaskSummary = {
  id: number
  task: Record<string, unknown>
  status: Status
  project: string | null
  assignee_id: string | null
  created_by: string | null
  parent_id: number | null
  key: string | null
  priority: number
  attempts: number
  /** Сколько предшественников ещё не закрыто: пока не ноль, задачу никто не заберёт. */
  waiting_on: number
  max_attempts: number | null
  retry_after: string | null
  lease_expires: string | null
  created_at: string
  updated_at: string
}

export type Task = TaskSummary & { result: unknown; depends_on: number[] }

export type TaskEvent = {
  id: number
  task_id: number
  at: string
  event: string
  actor: string | null
  from_status: Status | null
  to_status: Status | null
  detail: Record<string, unknown> | null
}

export type Stats = Record<Status | 'total', number>

type Envelope = {
  ok: boolean
  outcome: string
  message: string | null
  task?: Task
  tasks?: TaskSummary[]
  count?: number
  events?: TaskEvent[]
  stats?: Stats
  projects?: string[]
  assignees?: string[]
}

export class ApiError extends Error {
  outcome: string

  constructor(message: string, outcome: string) {
    super(message)
    this.outcome = outcome
  }
}

async function call(path: string, init?: RequestInit): Promise<Envelope> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new ApiError('ядро не отвечает — проверьте, что noted-api запущен', 'api_unavailable')
  }

  const body = (await response.json().catch(() => null)) as Envelope | null
  if (!body) throw new ApiError(`ядро ответило ${response.status} без тела`, 'internal_error')
  // `empty` и `not_found` — штатные исходы, но для дэшборда они тоже ошибки вызова,
  // поэтому решение принимает вызывающий: сюда долетает только ok=false.
  if (!body.ok) throw new ApiError(body.message ?? body.outcome, body.outcome)
  return body
}

export type Filters = {
  status: Status[]
  project: string | null
  assignee: string | null
  stale: number | null
  limit: number
}

export const NONE = '__none__'

function listQuery(filters: Filters): string {
  const query = new URLSearchParams()
  filters.status.forEach((s) => query.append('status', s))
  if (filters.project === NONE) query.set('unscoped', 'true')
  else if (filters.project) query.set('project', filters.project)
  if (filters.assignee === NONE) query.set('unassigned', 'true')
  else if (filters.assignee) query.set('assignee_id', filters.assignee)
  if (filters.stale) query.set('stale_seconds', String(filters.stale))
  query.set('limit', String(filters.limit))
  return query.toString()
}

export const api = {
  async list(filters: Filters): Promise<TaskSummary[]> {
    return (await call(`/api/tasks?${listQuery(filters)}`)).tasks ?? []
  },

  async overview(project: string | null) {
    const query = new URLSearchParams()
    if (project === NONE) query.set('unscoped', 'true')
    else if (project) query.set('project', project)
    const body = await call(`/api/stats?${query.toString()}`)
    return {
      stats: body.stats as Stats,
      projects: body.projects ?? [],
      assignees: body.assignees ?? [],
    }
  },

  async get(id: number): Promise<Task> {
    return (await call(`/api/tasks/${id}`)).task as Task
  },

  async events(id: number): Promise<TaskEvent[]> {
    return (await call(`/api/tasks/${id}/events`)).events ?? []
  },

  async children(parentId: number): Promise<TaskSummary[]> {
    return (await call(`/api/tasks?parent_id=${parentId}&limit=500`)).tasks ?? []
  },

  async create(input: {
    task: Record<string, unknown>
    project?: string | null
    assignee_id?: string | null
    priority?: number
    max_attempts?: number | null
  }): Promise<Task> {
    const body = await call('/api/tasks', {
      method: 'POST',
      body: JSON.stringify({ ...input, created_by: 'dashboard' }),
    })
    return body.task as Task
  },

  async setStatus(id: number, status: Status): Promise<Task> {
    const body = await call(`/api/tasks/${id}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    })
    return body.task as Task
  },
}

/** A thin client to the core. Response shapes come from the envelope in SPEC.md. */

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
  /** How many predecessors are still open: until zero, nobody can claim the task. */
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
    throw new ApiError('the core is not answering — check that noted-api is running', 'api_unavailable')
  }

  const body = (await response.json().catch(() => null)) as Envelope | null
  if (!body) throw new ApiError(`the core answered ${response.status} with no body`, 'internal_error')
  // `empty` and `not_found` are normal outcomes, but the dashboard treats them
  // as failed calls too, so only ok=false ever reaches this point.
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

function listQuery(filters: Filters, page: PageOptions): string {
  const query = new URLSearchParams()
  filters.status.forEach((s) => query.append('status', s))
  if (filters.project === NONE) query.set('unscoped', 'true')
  else if (filters.project) query.set('project', filters.project)
  if (filters.assignee === NONE) query.set('unassigned', 'true')
  else if (filters.assignee) query.set('assignee_id', filters.assignee)
  if (filters.stale) query.set('stale_seconds', String(filters.stale))
  if (page.beforeId) query.set('before_id', String(page.beforeId))
  query.set('limit', String(page.limit ?? filters.limit))
  return query.toString()
}

export type PageOptions = {
  limit?: number
  /** The cursor: the next page is everything older than this task. */
  beforeId?: number
}

export const api = {
  async login(token: string): Promise<void> {
    await call('/api/login', { method: 'POST', body: JSON.stringify({ token }) })
  },

  async logout(): Promise<void> {
    await call('/api/logout', { method: 'POST' })
  },

  async list(filters: Filters, page: PageOptions = {}): Promise<TaskSummary[]> {
    return (await call(`/api/tasks?${listQuery(filters, page)}`)).tasks ?? []
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
    // A human acting from the dashboard writes unconditionally, and says so:
    // agents get compare-and-set by default.
    const body = await call(`/api/tasks/${id}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status, force: true }),
    })
    return body.task as Task
  },
}

import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/select'
import { NONE, STATUSES, type Filters, type Stats, type Status } from '@/api'
import { STATUS_COLOR } from '@/lib/format'

type Props = {
  filters: Filters
  stats: Stats
  projects: string[]
  assignees: string[]
  onToggleStatus: (status: Status) => void
  onPatch: (patch: Partial<Filters>) => void
  onReset: () => void
}

const STALE_OPTIONS = [
  { value: '', label: 'неважно' },
  { value: '60', label: 'дольше минуты' },
  { value: '300', label: 'дольше 5 минут' },
  { value: '3600', label: 'дольше часа' },
  { value: '86400', label: 'дольше суток' },
]

export function FilterBar({
  filters,
  stats,
  projects,
  assignees,
  onToggleStatus,
  onPatch,
  onReset,
}: Props) {
  const dirty =
    filters.status.length > 0 || filters.project !== null || filters.assignee !== null || filters.stale !== null

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          size="sm"
          variant={filters.status.length === 0 ? 'default' : 'outline'}
          onClick={() => onPatch({ status: [] })}
        >
          все
          <span className="text-muted-foreground ml-1 tabular-nums">{stats.total}</span>
        </Button>

        {STATUSES.map((status) => {
          const active = filters.status.includes(status)
          return (
            <Button
              key={status}
              size="sm"
              color={STATUS_COLOR[status]}
              variant={active ? 'default' : 'outline'}
              aria-pressed={active}
              disabled={stats[status] === 0 && !active}
              onClick={() => onToggleStatus(status)}
            >
              {status}
              <span className="ml-1 tabular-nums opacity-70">{stats[status]}</span>
            </Button>
          )
        })}
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <Select
          className="w-44"
          triggerLabel="Проект"
          placeholder="проект: любой"
          value={filters.project ?? ''}
          onValueChange={(value) => onPatch({ project: value || null })}
          options={[
            { value: '', label: 'проект: любой' },
            { value: NONE, label: 'без проекта' },
            ...projects.map((name) => ({ value: name, label: name })),
          ]}
        />

        <Select
          className="w-48"
          triggerLabel="Исполнитель"
          placeholder="исполнитель: любой"
          value={filters.assignee ?? ''}
          onValueChange={(value) => onPatch({ assignee: value || null })}
          options={[
            { value: '', label: 'исполнитель: любой' },
            { value: NONE, label: 'общий пул' },
            ...assignees.map((name) => ({ value: name, label: name })),
          ]}
        />

        <Select
          className="w-48"
          triggerLabel="Не обновлялась дольше"
          placeholder="не обновлялась"
          value={filters.stale ? String(filters.stale) : ''}
          onValueChange={(value) => onPatch({ stale: value ? Number(value) : null })}
          options={STALE_OPTIONS}
        />

        {dirty && (
          <Button size="sm" variant="ghost" onClick={onReset}>
            сбросить
          </Button>
        )}
      </div>
    </div>
  )
}

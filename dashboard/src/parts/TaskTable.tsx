import { MoreHorizontal } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Empty } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { STATUSES, type Status, type TaskSummary } from '@/api'
import { STATUS_COLOR, ago, at, isStale, leaseLeft, title } from '@/lib/format'

type Props = {
  tasks: TaskSummary[]
  pending: boolean
  filtered: boolean
  onOpen: (id: number) => void
  onStatus: (id: number, status: Status) => void
  onProject: (project: string) => void
  onReset: () => void
  onCompose: () => void
}

export function TaskTable({
  tasks,
  pending,
  filtered,
  onOpen,
  onStatus,
  onProject,
  onReset,
  onCompose,
}: Props) {
  if (pending && tasks.length === 0) {
    return (
      <div className="flex flex-col gap-2 py-2">
        {[0, 1, 2, 3, 4].map((row) => (
          <Skeleton key={row} className="h-11 w-full" />
        ))}
      </div>
    )
  }

  if (tasks.length === 0) {
    return (
      <Empty
        bordered
        title={filtered ? 'Под фильтры ничего не попало' : 'Задач пока нет'}
        description={
          filtered
            ? 'Снимите часть условий — или сбросьте фильтры целиком.'
            : 'Поставьте задачу формой выше либо инструментом set_task из агента.'
        }
        action={
          filtered ? (
            <Button variant="outline" size="sm" onClick={onReset}>
              Сбросить фильтры
            </Button>
          ) : (
            <Button size="sm" onClick={onCompose}>
              Поставить задачу
            </Button>
          )
        }
      />
    )
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-14">#</TableHead>
          <TableHead className="w-32">статус</TableHead>
          <TableHead className="w-32">проект</TableHead>
          <TableHead>задача</TableHead>
          <TableHead className="w-36">исполнитель</TableHead>
          <TableHead className="w-32">обновлена</TableHead>
          <TableHead className="w-12" />
        </TableRow>
      </TableHeader>

      <TableBody>
        {tasks.map((task) => (
          <TableRow key={task.id} className="group">
            <TableCell className="text-muted-foreground font-mono text-xs tabular-nums">
              {task.id}
            </TableCell>

            <TableCell>
              <Badge color={STATUS_COLOR[task.status]} size="sm">
                {task.status}
              </Badge>
            </TableCell>

            <TableCell className="font-mono text-xs">
              {task.project ? (
                <button
                  type="button"
                  className="hover:text-foreground text-muted-foreground max-w-full truncate"
                  onClick={() => onProject(task.project as string)}
                >
                  {task.project}
                </button>
              ) : (
                <span className="text-muted-foreground/60">—</span>
              )}
            </TableCell>

            <TableCell className="min-w-0">
              <button
                type="button"
                className="hover:text-primary text-left font-medium break-words"
                onClick={() => onOpen(task.id)}
              >
                {title(task)}
              </button>
              {task.parent_id && (
                <span className="text-muted-foreground ml-2 font-mono text-xs">
                  из #{task.parent_id}
                </span>
              )}
              {isStale(task) && (
                <Badge color="amber" variant="outline" size="sm" className="ml-2">
                  {task.lease_expires ? 'аренда истекла' : 'залипла'}
                </Badge>
              )}
              {task.waiting_on > 0 && (
                <Badge color="violet" variant="outline" size="sm" className="ml-2">
                  ждёт {task.waiting_on}
                </Badge>
              )}
              {task.attempts > 1 && (
                <span className="text-muted-foreground ml-2 font-mono text-xs">
                  попытка {task.attempts}
                  {task.max_attempts ? ` из ${task.max_attempts}` : ''}
                </span>
              )}
              {task.priority !== 0 && (
                <span className="text-muted-foreground ml-2 font-mono text-xs">
                  приоритет {task.priority}
                </span>
              )}
            </TableCell>

            <TableCell className="text-muted-foreground font-mono text-xs">
              {task.assignee_id ?? 'общий пул'}
            </TableCell>

            <TableCell className="text-muted-foreground font-mono text-xs" title={at(task.updated_at)}>
              {ago(task.updated_at)}
              {leaseLeft(task) && <span className="block opacity-70">аренда {leaseLeft(task)}</span>}
            </TableCell>

            <TableCell>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Действия над задачей ${task.id}`}
                    className="opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                  >
                    <MoreHorizontal />
                  </Button>
                </DropdownMenuTrigger>

                <DropdownMenuContent align="end">
                  <DropdownMenuItem onSelect={() => onOpen(task.id)}>Открыть</DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuLabel>Сменить статус</DropdownMenuLabel>
                  {STATUSES.filter((status) => status !== task.status).map((status) => (
                    <DropdownMenuItem key={status} onSelect={() => onStatus(task.id, status)}>
                      {status}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

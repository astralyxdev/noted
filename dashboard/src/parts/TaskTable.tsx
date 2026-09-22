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
        title={filtered ? 'Nothing matches these filters' : 'No tasks yet'}
        description={
          filtered
            ? 'Drop some conditions, or clear the filters entirely.'
            : 'Create one with the button above, or with set_task from an agent.'
        }
        action={
          filtered ? (
            <Button variant="outline" size="sm" onClick={onReset}>
              Clear filters
            </Button>
          ) : (
            <Button size="sm" onClick={onCompose}>
              Create a task
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
          <TableHead className="w-32">status</TableHead>
          <TableHead className="w-32">project</TableHead>
          <TableHead>task</TableHead>
          <TableHead className="w-36">assignee</TableHead>
          <TableHead className="w-32">updated</TableHead>
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
                  from #{task.parent_id}
                </span>
              )}
              {isStale(task) && (
                <Badge color="amber" variant="outline" size="sm" className="ml-2">
                  {task.lease_expires ? 'lease expired' : 'stuck'}
                </Badge>
              )}
              {task.waiting_on > 0 && (
                <Badge color="violet" variant="outline" size="sm" className="ml-2">
                  waiting on {task.waiting_on}
                </Badge>
              )}
              {task.attempts > 1 && (
                <span className="text-muted-foreground ml-2 font-mono text-xs">
                  attempt {task.attempts}
                  {task.max_attempts ? ` of ${task.max_attempts}` : ''}
                </span>
              )}
              {task.priority !== 0 && (
                <span className="text-muted-foreground ml-2 font-mono text-xs">
                  priority {task.priority}
                </span>
              )}
            </TableCell>

            <TableCell className="text-muted-foreground font-mono text-xs">
              {task.assignee_id ?? 'shared pool'}
            </TableCell>

            <TableCell className="text-muted-foreground font-mono text-xs" title={at(task.updated_at)}>
              {ago(task.updated_at)}
              {leaseLeft(task) && <span className="block opacity-70">lease {leaseLeft(task)}</span>}
            </TableCell>

            <TableCell>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Actions for task ${task.id}`}
                    className="opacity-0 transition-opacity group-hover:opacity-100 focus-visible:opacity-100"
                  >
                    <MoreHorizontal />
                  </Button>
                </DropdownMenuTrigger>

                <DropdownMenuContent align="end">
                  <DropdownMenuItem onSelect={() => onOpen(task.id)}>Open</DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuLabel>Change status</DropdownMenuLabel>
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

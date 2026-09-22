import { useEffect, useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Select } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Spinner } from '@/components/ui/spinner'
import { STATUSES, api, type Status, type Task, type TaskEvent, type TaskSummary } from '@/api'
import { EVENT_LABEL, STATUS_COLOR, at, ago, leaseLeft, pretty, title } from '@/lib/format'

type Props = {
  taskId: number | null
  onClose: () => void
  onStatus: (id: number, status: Status) => void
  onOpen: (id: number) => void
  /** Меняется после каждого обновления — карточка перечитывает себя. */
  revision: number
}

export function TaskDialog({ taskId, onClose, onStatus, onOpen, revision }: Props) {
  const [task, setTask] = useState<Task | null>(null)
  const [children, setChildren] = useState<TaskSummary[]>([])
  const [log, setLog] = useState<TaskEvent[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (taskId === null) {
      setTask(null)
      setChildren([])
      setLog([])
      setError(null)
      return
    }
    let alive = true
    void (async () => {
      try {
        const [loaded, kids, history] = await Promise.all([
          api.get(taskId),
          api.children(taskId),
          api.events(taskId),
        ])
        if (!alive) return
        setTask(loaded)
        setChildren(kids)
        setLog(history)
        setError(null)
      } catch (cause) {
        if (alive) setError(cause instanceof Error ? cause.message : String(cause))
      }
    })()
    return () => {
      alive = false
    }
  }, [taskId, revision])

  return (
    <Dialog open={taskId !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent size="xl">
        {error && (
          <DialogBody>
            <p className="text-destructive text-sm">{error}</p>
          </DialogBody>
        )}

        {!task && !error && (
          <DialogBody>
            <div className="text-muted-foreground flex items-center gap-2 py-6 text-sm">
              <Spinner size="sm" /> Загружаю задачу…
            </div>
          </DialogBody>
        )}

        {task && (
          <>
            <DialogHeader>
              <div className="flex flex-wrap items-center gap-2">
                <Badge color={STATUS_COLOR[task.status]} size="sm">
                  {task.status}
                </Badge>
                <span className="text-muted-foreground font-mono text-xs">задача #{task.id}</span>
                {task.parent_id && (
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-foreground font-mono text-xs"
                    onClick={() => onOpen(task.parent_id as number)}
                  >
                    из #{task.parent_id}
                  </button>
                )}
              </div>
              <DialogTitle>{title(task)}</DialogTitle>
            </DialogHeader>

            <DialogBody className="flex flex-col gap-5">
              <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-sm">
                <Fact label="проект" value={task.project ?? '—'} />
                <Fact label="исполнитель" value={task.assignee_id ?? 'общий пул'} />
                <Fact label="поставил" value={task.created_by ?? '—'} />
                <Fact
                  label="попытки"
                  value={task.max_attempts ? `${task.attempts} из ${task.max_attempts}` : String(task.attempts)}
                />
                {task.priority !== 0 && <Fact label="приоритет" value={String(task.priority)} />}
                {leaseLeft(task) && <Fact label="аренда" value={`осталось ${leaseLeft(task)}`} />}
                {task.retry_after && <Fact label="повтор после" value={at(task.retry_after)} />}
                <Fact label="создана" value={at(task.created_at)} />
                <Fact label="обновлена" value={at(task.updated_at)} />
                {task.key && <Fact label="ключ" value={task.key} />}
              </dl>

              <section className="flex flex-col gap-2">
                <h3 className="text-muted-foreground font-mono text-xs uppercase">задание</h3>
                <pre className="bg-muted overflow-x-auto rounded-lg p-3 font-mono text-xs">
                  {pretty(task.task)}
                </pre>
              </section>

              <section className="flex flex-col gap-2">
                <h3 className="text-muted-foreground font-mono text-xs uppercase">результат</h3>
                {task.result === null ? (
                  <p className="text-muted-foreground text-sm">
                    Исполнитель ещё не отчитался — он пишет сюда через <code>set_status</code>.
                  </p>
                ) : (
                  <pre className="bg-muted overflow-x-auto rounded-lg p-3 font-mono text-xs">
                    {pretty(task.result)}
                  </pre>
                )}
              </section>

              {task.depends_on.length > 0 && (
                <section className="flex flex-col gap-2">
                  <h3 className="text-muted-foreground font-mono text-xs uppercase">
                    ждёт задач · {task.depends_on.length}
                  </h3>
                  <ul className="flex flex-wrap gap-2">
                    {task.depends_on.map((dep) => (
                      <li key={dep}>
                        <button
                          type="button"
                          className="hover:text-primary font-mono text-sm"
                          onClick={() => onOpen(dep)}
                        >
                          #{dep}
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {children.length > 0 && (
                <section className="flex flex-col gap-2">
                  <h3 className="text-muted-foreground font-mono text-xs uppercase">
                    подзадачи · {children.length}
                  </h3>
                  <ul className="flex flex-col">
                    {children.map((child) => (
                      <li key={child.id} className="flex items-center gap-2 py-1.5 text-sm">
                        <Badge color={STATUS_COLOR[child.status]} size="sm">
                          {child.status}
                        </Badge>
                        <button
                          type="button"
                          className="hover:text-primary text-left"
                          onClick={() => onOpen(child.id)}
                        >
                          {title(child)}
                        </button>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {log.length > 0 && (
                <section className="flex flex-col gap-2">
                  <h3 className="text-muted-foreground font-mono text-xs uppercase">
                    журнал · {log.length}
                  </h3>
                  <ol className="flex flex-col gap-1.5">
                    {log.map((entry) => (
                      <li key={entry.id} className="flex flex-wrap items-baseline gap-x-2 text-sm">
                        <span className="text-muted-foreground font-mono text-xs" title={at(entry.at)}>
                          {ago(entry.at)}
                        </span>
                        <span>{EVENT_LABEL[entry.event] ?? entry.event}</span>
                        {entry.from_status && entry.to_status && (
                          <span className="text-muted-foreground font-mono text-xs">
                            {entry.from_status} → {entry.to_status}
                          </span>
                        )}
                        {entry.actor && (
                          <span className="text-muted-foreground font-mono text-xs">{entry.actor}</span>
                        )}
                      </li>
                    ))}
                  </ol>
                </section>
              )}

              <Separator />

              <div className="flex flex-wrap items-center gap-2">
                <Select
                  className="w-44"
                  triggerLabel="Новый статус"
                  value={task.status}
                  onValueChange={(status) => onStatus(task.id, status as Status)}
                  options={STATUSES.map((status) => ({ value: status, label: status }))}
                />
                <Button variant="outline" onClick={onClose}>
                  Закрыть
                </Button>
              </div>
            </DialogBody>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-muted-foreground font-mono text-xs uppercase">{label}</dt>
      <dd className="break-words">{value}</dd>
    </>
  )
}

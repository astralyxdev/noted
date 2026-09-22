import { useCallback, useState } from 'react'

import { Plus } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Separator } from '@/components/ui/separator'
import { Wordmark } from '@/components/ui/wordmark'
import { Stat } from '@/components/ui/stat'
import { useToast } from '@/components/ui/toast'
import { api, type Status } from '@/api'
import { ConnectButton, ConnectDialog } from '@/parts/ConnectDialog'
import { LoginScreen } from '@/parts/LoginScreen'
import { NewTaskDialog } from '@/parts/NewTaskDialog'
import { FilterBar } from '@/parts/FilterBar'
import { TaskDialog } from '@/parts/TaskDialog'
import { TaskTable } from '@/parts/TaskTable'
import { cn } from '@/lib/utils'
import { isStale } from '@/lib/format'
import { Skeleton } from '@/components/ui/skeleton'
import {
  openTaskFromUrl,
  useDashboard,
  useFilters,
  useLive,
  useNearBottom,
  useTicker,
  useUrlSync,
} from '@/hooks'

const LIVE_LABEL: Record<string, string> = {
  connecting: 'подключаюсь…',
  live: '',
  down: 'нет связи с ядром',
}

export function App() {
  const { filters, patch, toggleStatus, setFilters } = useFilters()
  const [openTask, setOpenTask] = useState<number | null>(openTaskFromUrl)
  const [composing, setComposing] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [revision, setRevision] = useState(0)

  const { tasks, stats, projects, assignees, error, locked, pending, more, loadingMore, loadMore, refresh } =
    useDashboard(filters)
  const { toast } = useToast()
  useTicker()
  useUrlSync(filters, openTask)

  const reload = useCallback(() => {
    setRevision((n) => n + 1)
    void refresh()
  }, [refresh])

  const live = useLive(reload)
  const bottom = useNearBottom(more && !pending, loadMore)

  const changeStatus = useCallback(
    async (id: number, status: Status) => {
      try {
        await api.setStatus(id, status)
        toast({ title: `Задача #${id} → ${status}`, color: 'neutral' })
        reload()
      } catch (cause) {
        toast({
          title: 'Статус не сменился',
          description: cause instanceof Error ? cause.message : undefined,
          color: 'destructive',
        })
      }
    },
    [reload, toast],
  )

  const reset = useCallback(
    () => setFilters({ status: [], project: null, assignee: null, stale: null, limit: filters.limit }),
    [filters.limit, setFilters],
  )

  const filtered =
    filters.status.length > 0 || filters.project !== null || filters.assignee !== null || filters.stale !== null
  const stuck = tasks.filter(isStale).length

  if (locked) return <LoginScreen onEntered={reload} />

  return (
    <div className="bg-background text-foreground min-h-dvh">
      <header className="border-border bg-background sticky top-0 z-20 border-b">
        <div className="mx-auto flex h-13 max-w-6xl items-center justify-between gap-4 px-5">
          <a
            href="/"
            aria-label="Noted, к списку задач"
            className="focus-visible:ring-ring flex items-center gap-2.5 rounded-lg px-1 py-1 focus-visible:ring-2 focus-visible:outline-none"
          >
            <Wordmark className="h-5" />
            {/* A vertical separator stretches full height by default
                (self-stretch), which puts it off the centre of its siblings. */}
            <Separator orientation="vertical" className="h-5 self-center" />
            <span className="text-[0.95rem] font-medium tracking-tight">Noted</span>
          </a>

          <div className="flex items-center gap-3">
            {/* While the connection holds the indicator stays quiet: nothing to report. */}
            <span
              className={cn(
                'text-muted-foreground flex items-center gap-1.5 font-mono text-xs',
                live === 'live' && 'sr-only',
              )}
            >
              <span
                className={cn(
                  'size-1.5 rounded-full',
                  live === 'down' ? 'bg-amber' : 'bg-muted-foreground',
                )}
              />
              {LIVE_LABEL[live]}
            </span>

            <ConnectButton onClick={() => setConnecting(true)} />

            <Button size="sm" onClick={() => setComposing(true)}>
              <Plus />
              Новая задача
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto flex max-w-6xl flex-col gap-5 px-5 py-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat bordered animate={false} size="sm" label="всего" value={stats.total} />
          <Stat bordered animate={false} size="sm" label="в работе" value={stats.in_progress} />
          <Stat bordered animate={false} size="sm" label="в очереди" value={stats.pending} />
          <Stat
            bordered
            animate={false}
            size="sm"
            label="залипло"
            value={stuck}
            hint={stuck > 0 ? 'дольше 5 минут без обновления' : undefined}
          />
        </div>

        <FilterBar
          filters={filters}
          stats={stats}
          projects={projects}
          assignees={assignees}
          onToggleStatus={toggleStatus}
          onPatch={patch}
          onReset={reset}
        />

        {error && (
          <p className="border-destructive/40 bg-destructive/5 text-destructive rounded-lg border px-3 py-2 text-sm">
            {error}
          </p>
        )}

        <TaskTable
          tasks={tasks}
          pending={pending}
          filtered={filtered}
          onOpen={setOpenTask}
          onStatus={changeStatus}
          onProject={(project) => patch({ project })}
          onReset={reset}
          onCompose={() => setComposing(true)}
        />

        {/* The loading anchor: once it crosses the screen, pull the next page. */}
        {more && <div ref={bottom} aria-hidden="true" className="h-1" />}

        {loadingMore && (
          <div className="flex flex-col gap-2" aria-live="polite">
            <Skeleton className="h-11 w-full" />
            <Skeleton className="h-11 w-full" />
          </div>
        )}

        <p className="text-muted-foreground flex justify-between font-mono text-xs">
          <span>
            показано {tasks.length} из {stats.total}
          </span>
          {!more && tasks.length > 0 && <span>всё показано</span>}
        </p>
      </main>

      <ConnectDialog open={connecting} onOpenChange={setConnecting} />

      <NewTaskDialog
        open={composing}
        onOpenChange={setComposing}
        project={filters.project}
        onCreated={reload}
      />

      <TaskDialog
        taskId={openTask}
        revision={revision}
        onClose={() => setOpenTask(null)}
        onOpen={setOpenTask}
        onStatus={changeStatus}
      />
    </div>
  )
}

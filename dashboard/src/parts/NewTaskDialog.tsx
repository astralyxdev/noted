import { useEffect, useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useToast } from '@/components/ui/toast'
import { api } from '@/api'

type Props = {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The project from the active filter, so a new task lands in the same scope. */
  project: string | null
  onCreated: () => void
}

export function NewTaskDialog({ open, onOpenChange, project, onCreated }: Props) {
  const { toast } = useToast()
  const [title, setTitle] = useState('')
  const [assignee, setAssignee] = useState('')
  const [scope, setScope] = useState('')
  const [payload, setPayload] = useState('')
  const [priority, setPriority] = useState('0')
  const [attempts, setAttempts] = useState('')
  const [payloadError, setPayloadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // The form lives only while open: the next opening starts clean, except for
  // the project, which is taken from the active filter.
  useEffect(() => {
    if (!open) return
    setTitle('')
    setAssignee('')
    setPayload('')
    setPayloadError(null)
    setPriority('0')
    setAttempts('')
    setScope(project ?? '')
  }, [open, project])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!title.trim() || saving) return

    let task: Record<string, unknown> = { title: title.trim() }
    if (payload.trim()) {
      try {
        const extra: unknown = JSON.parse(payload)
        if (typeof extra !== 'object' || extra === null || Array.isArray(extra)) {
          throw new Error('a JSON object is required, not an array or a value')
        }
        task = { ...task, ...(extra as Record<string, unknown>) }
      } catch (cause) {
        setPayloadError(cause instanceof Error ? cause.message : 'could not parse JSON')
        return
      }
    }

    setPayloadError(null)
    setSaving(true)
    try {
      const created = await api.create({
        task,
        project: scope.trim() || null,
        assignee_id: assignee.trim() || null,
        priority: Number(priority) || 0,
        max_attempts: attempts.trim() ? Math.max(1, Number(attempts)) : null,
      })
      toast({ title: `Task #${created.id} created`, color: 'green' })
      onOpenChange(false)
      onCreated()
    } catch (cause) {
      toast({
        title: 'Could not create the task',
        description: cause instanceof Error ? cause.message : undefined,
        color: 'destructive',
      })
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <form onSubmit={submit}>
          <DialogHeader>
            <DialogTitle>New task</DialogTitle>
            <DialogDescription>
              An empty assignee means the shared pool — the first free agent picks it up.
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="task-title">What needs doing</Label>
              <Input
                id="task-title"
                autoFocus
                required
                maxLength={200}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="Index the repository"
              />
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="task-project">Project</Label>
                <Input
                  id="task-project"
                  className="font-mono text-xs"
                  value={scope}
                  onChange={(event) => setScope(event.target.value)}
                  placeholder="no project"
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <Label htmlFor="task-assignee">Assignee</Label>
                <Input
                  id="task-assignee"
                  className="font-mono text-xs"
                  value={assignee}
                  onChange={(event) => setAssignee(event.target.value)}
                  placeholder="shared pool"
                />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="task-priority">Priority</Label>
                <Input
                  id="task-priority"
                  type="number"
                  value={priority}
                  onChange={(event) => setPriority(event.target.value)}
                />
                <p className="text-muted-foreground text-xs">Higher goes out sooner.</p>
              </div>

              <div className="flex flex-col gap-1.5">
                <Label htmlFor="task-attempts">Attempts</Label>
                <Input
                  id="task-attempts"
                  type="number"
                  min={1}
                  value={attempts}
                  onChange={(event) => setAttempts(event.target.value)}
                  placeholder="no retries"
                />
                <p className="text-muted-foreground text-xs">A failure returns it to the queue after a pause.</p>
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="task-payload">JSON payload — optional</Label>
              <Textarea
                id="task-payload"
                rows={5}
                spellCheck={false}
                className="font-mono text-xs"
                value={payload}
                onChange={(event) => setPayload(event.target.value)}
                placeholder={'{"repo": "astralyx/noted", "files": ["main.py"]}'}
                error={Boolean(payloadError)}
              />
              {payloadError ? (
                <p className="text-destructive text-xs">{payloadError}</p>
              ) : (
                <p className="text-muted-foreground text-xs">
                  Merged with the title, so the agent receives one object.
                </p>
              )}
            </div>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!title.trim() || saving}>
              Create
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

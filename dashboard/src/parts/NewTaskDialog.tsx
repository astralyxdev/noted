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
  /** Проект из текущего фильтра — новая задача попадает в тот же скоуп. */
  project: string | null
  onCreated: () => void
}

export function NewTaskDialog({ open, onOpenChange, project, onCreated }: Props) {
  const { toast } = useToast()
  const [title, setTitle] = useState('')
  const [assignee, setAssignee] = useState('')
  const [scope, setScope] = useState('')
  const [payload, setPayload] = useState('')
  const [payloadError, setPayloadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // Форма живёт только пока открыта: следующее открытие начинается с чистого листа,
  // но проект подставляется из текущего фильтра.
  useEffect(() => {
    if (!open) return
    setTitle('')
    setAssignee('')
    setPayload('')
    setPayloadError(null)
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
          throw new Error('нужен JSON-объект, а не массив или значение')
        }
        task = { ...task, ...(extra as Record<string, unknown>) }
      } catch (cause) {
        setPayloadError(cause instanceof Error ? cause.message : 'не разобрать JSON')
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
      })
      toast({ title: `Задача #${created.id} поставлена`, color: 'green' })
      onOpenChange(false)
      onCreated()
    } catch (cause) {
      toast({
        title: 'Не удалось поставить задачу',
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
            <DialogTitle>Новая задача</DialogTitle>
            <DialogDescription>
              Пустой исполнитель означает общий пул — задачу заберёт первый свободный агент.
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="task-title">Что нужно сделать</Label>
              <Input
                id="task-title"
                autoFocus
                required
                maxLength={200}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="Проиндексировать репозиторий"
              />
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="task-project">Проект</Label>
                <Input
                  id="task-project"
                  className="font-mono text-xs"
                  value={scope}
                  onChange={(event) => setScope(event.target.value)}
                  placeholder="без проекта"
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <Label htmlFor="task-assignee">Исполнитель</Label>
                <Input
                  id="task-assignee"
                  className="font-mono text-xs"
                  value={assignee}
                  onChange={(event) => setAssignee(event.target.value)}
                  placeholder="общий пул"
                />
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="task-payload">JSON-нагрузка — необязательно</Label>
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
                  Сливается с заголовком — агент получит один объект.
                </p>
              )}
            </div>
          </DialogBody>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Отмена
            </Button>
            <Button type="submit" disabled={!title.trim() || saving}>
              Поставить
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

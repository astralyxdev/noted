import { useState } from 'react'
import { Check, Copy, Plug } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Separator } from '@/components/ui/separator'

type Props = { open: boolean; onOpenChange: (open: boolean) => void }

/** Кусок, который нужно скопировать целиком, с кнопкой копирования. */
function Snippet({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // Буфер недоступен (не-https, отказ в правах) — текст остаётся на виду,
      // его можно выделить руками.
    }
  }

  return (
    <div className="relative">
      <pre className="bg-muted overflow-x-auto rounded-lg py-3 pr-12 pl-3 font-mono text-xs whitespace-pre-wrap">
        {text}
      </pre>
      <Button
        variant="ghost"
        size="icon-sm"
        className="absolute top-1.5 right-1.5"
        onClick={copy}
        aria-label={copied ? 'Скопировано' : 'Скопировать'}
      >
        {copied ? <Check /> : <Copy />}
      </Button>
    </div>
  )
}

export function ConnectDialog({ open, onOpenChange }: Props) {
  // Адрес берём из текущей страницы: дэшборд и MCP живут на одном порту.
  const base = window.location.origin
  const url = `${base}/mcp/`

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Подключить MCP</DialogTitle>
          <DialogDescription>
            Сервер поднят вместе с ядром и слушает на том же порту — отдельный процесс-адаптер не нужен.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="flex flex-col gap-5">
          <section className="flex flex-col gap-2">
            <h3 className="text-muted-foreground font-mono text-xs uppercase">адрес сервера</h3>
            <Snippet text={url} />
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-muted-foreground font-mono text-xs uppercase">claude code</h3>
            <Snippet text={`claude mcp add --transport http noted ${url}`} />
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-muted-foreground font-mono text-xs uppercase">конфиг клиента</h3>
            <Snippet
              text={JSON.stringify(
                { mcpServers: { noted: { type: 'http', url } } },
                null,
                2,
              )}
            />
          </section>

          <Separator />

          <section className="text-muted-foreground flex flex-col gap-2 text-sm">
            <p>
              Инструменты: <code className="font-mono text-xs">set_task</code>,{' '}
              <code className="font-mono text-xs">get_tasks</code>,{' '}
              <code className="font-mono text-xs">get_task</code>,{' '}
              <code className="font-mono text-xs">set_status</code>,{' '}
              <code className="font-mono text-xs">claim_task</code>.
            </p>
            <p>
              Если задан <code className="font-mono text-xs">NOTED_TOKEN</code>, добавьте заголовок:{' '}
              <code className="font-mono text-xs">--header "X-Noted-Token: …"</code>.
            </p>
            <p>
              Клиент без HTTP-транспорта подключается через stdio-адаптер:{' '}
              <code className="font-mono text-xs">noted-mcp</code> из того же образа.
            </p>
          </section>
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

export function ConnectButton({ onClick }: { onClick: () => void }) {
  return (
    <Button variant="outline" size="sm" onClick={onClick}>
      <Plug />
      Подключить MCP
    </Button>
  )
}

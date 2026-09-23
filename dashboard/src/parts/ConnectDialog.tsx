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

/** A snippet meant to be copied whole, with a copy button. */
function Snippet({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // The clipboard is unavailable (non-https, denied permission) — the text
      // stays visible and can still be selected by hand.
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
        aria-label={copied ? 'Copied' : 'Copy'}
      >
        {copied ? <Check /> : <Copy />}
      </Button>
    </div>
  )
}

export function ConnectDialog({ open, onOpenChange }: Props) {
  // The address comes from the current page: dashboard and MCP share a port.
  const base = window.location.origin
  const url = `${base}/mcp/`

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Connect MCP</DialogTitle>
          <DialogDescription>
            The server comes up with the core and listens on the same port — no separate adapter process is needed.
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="flex flex-col gap-5">
          <section className="flex flex-col gap-2">
            <h3 className="text-muted-foreground font-mono text-xs uppercase">server address</h3>
            <Snippet text={url} />
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-muted-foreground font-mono text-xs uppercase">claude code</h3>
            <Snippet text={`claude mcp add --transport http noted ${url}`} />
          </section>

          <section className="flex flex-col gap-2">
            <h3 className="text-muted-foreground font-mono text-xs uppercase">client config</h3>
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
              Tools: <code className="font-mono text-xs">set_task</code>,{' '}
              <code className="font-mono text-xs">get_tasks</code>,{' '}
              <code className="font-mono text-xs">get_task</code>,{' '}
              <code className="font-mono text-xs">set_status</code>,{' '}
              <code className="font-mono text-xs">claim_task</code>.
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
      Connect MCP
    </Button>
  )
}

import { useState, type FormEvent } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Wordmark } from '@/components/ui/wordmark'
import { api } from '@/api'

/**
 * The dashboard door.
 *
 * Agents arrive with a key in a header; a browser sends no headers, so the
 * dashboard gets its own entrance and its own cookie. Without it a core with
 * NOTED_TOKEN set would answer the browser 401 on every request.
 */
export function LoginScreen({ onEntered }: { onEntered: () => void }) {
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!token.trim() || busy) return
    setBusy(true)
    try {
      await api.login(token.trim())
      setError(null)
      onEntered()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'could not sign in')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-background text-foreground grid min-h-dvh place-items-center px-5">
      <form onSubmit={submit} className="flex w-full max-w-sm flex-col gap-5">
        <div className="flex items-center gap-2.5">
          <Wordmark className="h-5" />
          <span className="text-[0.95rem] font-medium tracking-tight">Noted</span>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="token">Token</Label>
          <Input
            id="token"
            type="password"
            autoFocus
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="NOTED_TOKEN"
            error={Boolean(error)}
          />
          {error ? (
            <p className="text-destructive text-xs">{error}</p>
          ) : (
            <p className="text-muted-foreground text-xs">
              The same token the core was given in <code className="font-mono">NOTED_TOKEN</code>.
            </p>
          )}
        </div>

        <Button type="submit" disabled={!token.trim() || busy}>
          Sign in
        </Button>
      </form>
    </div>
  )
}

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { ToastProvider } from '@/components/ui/toast'
import { App } from '@/App'

import './index.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ToastProvider>
      <App />
    </ToastProvider>
  </StrictMode>,
)

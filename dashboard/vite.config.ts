import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Ядро отдаёт собранный dist само, поэтому в разработке просто проксируем
// к нему API и поток событий — фронтенд везде живёт на тех же путях.
const core = process.env.NOTED_API ?? 'http://127.0.0.1:8787'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: core, changeOrigin: true },
      '/events': { target: core, changeOrigin: true },
    },
  },
})

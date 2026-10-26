import { defineConfig } from 'vite'
import { execFileSync } from 'node:child_process'

let version = 'dev'
try { version = execFileSync('git', ['rev-list', '--count', 'HEAD'], { encoding: 'utf8' }).trim() } catch {}

export default defineConfig({
  define: { __APP_VERSION__: JSON.stringify(version) },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:28000' }
  },
  build: { outDir: 'dist', emptyOutDir: true }
})

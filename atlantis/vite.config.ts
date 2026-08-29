import react from '@vitejs/plugin-react-swc'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import { resolve } from 'path'

const frontendDir = resolve(__dirname, 'app/frontend')

// Atlantis port of the Fallout frontend: same entrypoint, same aliases, same
// PostCSS/Tailwind v4 pipeline. No Rails + no Sentry upload plugin (the
// bundle still references __SENTRY_RELEASE__ via `define`, like Fallout).
export default defineConfig({
  root: frontendDir,
  publicDir: resolve(__dirname, 'public'),
  plugins: [react(), tailwindcss()],
  define: {
    __SENTRY_RELEASE__: JSON.stringify(null),
  },
  resolve: {
    alias: {
      '@': frontendDir,
      '~': frontendDir,
    },
  },
  build: {
    outDir: resolve(__dirname, 'dist'),
    manifest: 'manifest.json',
    emptyOutDir: true,
    rollupOptions: {
      input: resolve(frontendDir, 'entrypoints/inertia.ts'),
    },
    sourcemap: true,
  },
  server: {
    port: 3039,
    strictPort: true,
  },
})
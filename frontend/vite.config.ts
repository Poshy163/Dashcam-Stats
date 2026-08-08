import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// In production the FastAPI process serves this build from /app/frontend/dist, so the
// app and its API share an origin and there is nothing to configure. `server.proxy`
// exists only to reproduce that during `npm run dev`, where Vite and uvicorn are
// separate processes on different ports.
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8080'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
      '/health': { target: API_TARGET, changeOrigin: true },
      // Video and thumbnails stream from the backend; without this, playback and
      // range requests break in dev but work in production, which is a confusing bug
      // to chase later.
      '/media': { target: API_TARGET, changeOrigin: true },
      '/stream': { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Leaflet and the charting library are large and change rarely; splitting them
        // out keeps the app chunk small across redeploys.
        manualChunks: {
          map: ['leaflet', 'react-leaflet'],
          charts: ['recharts'],
        },
      },
    },
  },
})

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
    // No `manualChunks`, deliberately.
    //
    // Naming `['leaflet', 'react-leaflet']` as a chunk looked like it would keep the map
    // out of the app bundle, and did the opposite. `react-leaflet` imports React, so React
    // was pulled into that chunk with it; the entry then has to import the chunk to get
    // React, so `index.html` emitted a `modulepreload` for it and every visitor downloaded
    // ~91 kB gzip of Leaflet before first paint whether or not they ever opened a map. The
    // route-level `React.lazy` split in App.tsx was defeated for the single largest
    // dependency in the app, and recharts was dragged in behind React too -- its "own"
    // chunk built out at 400 bytes.
    //
    // Vite's default splitting already gives every lazily-imported route its own chunk and
    // hoists genuinely shared code, which is what was wanted in the first place.
  },
})

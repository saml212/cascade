import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  server: {
    port: 8421,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://localhost:8420', changeOrigin: false },
      '/media': { target: 'http://localhost:8420', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
    target: 'es2022',
  },
});

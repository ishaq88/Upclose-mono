import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  // Server-only setting: never expose infrastructure addresses as VITE_* values.
  const env = loadEnv(mode, process.cwd(), 'API_PROXY_TARGET');
  const target = process.env.API_PROXY_TARGET ?? env.API_PROXY_TARGET ?? 'http://127.0.0.1:8000';

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      strictPort: true,
      proxy: {
        '/api/v1': { target },
        // Share links only; a bare '/r' prefix would also swallow /report and /reports.
        '^/r/': { target },
      },
    },
  };
});

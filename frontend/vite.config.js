// vite 配置：dev 代理 /api → 127.0.0.1:8787；build 输出到 backend/static（FastAPI 静态挂载）
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 输出到后端静态目录，后端直接挂载工作台页面
    outDir: path.resolve(__dirname, '../backend/static'),
    emptyOutDir: true,
  },
})

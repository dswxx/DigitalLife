/** 前端构建与开发代理；正式使用时由 Python 后端托管 dist 并注入启动令牌。 */
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 原生 ESM 配置，避免构建配置时额外启动编译进程。
export default defineConfig({
  plugins: [vue()],
  // 后端在同一个进程里托管 dist，所以资源用相对路径就够，不写死主机名
  base: './',
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
  },
  server: {
    // 开发时（npm run dev）把接口转发到后端，省得在前端里写死后端地址
    proxy: { '/api': 'http://127.0.0.1:8787' },
  },
})

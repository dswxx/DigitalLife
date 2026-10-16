import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/*
 * 这里是 `.mjs` 而不是 `.ts`，是有原因的：
 * vite 遇到 .ts 配置会先用它的打包器**编译一遍**，而那个打包器在 Windows 上
 * 会 spawn 一次 `net use`（只为解析网络映射盘的真实路径）。在受限环境里
 * （不许起管道子进程）那一步会 EPERM，整次构建直接失败。
 * 写成原生 ESM 就能被直接 import，绕开那一步。
 */
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

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 에이전트 API 서버(FastAPI). back/api 를 8000 포트로 띄운다.
      "/api": "http://127.0.0.1:8000",
    },
  },
});

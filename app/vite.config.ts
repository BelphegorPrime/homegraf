import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

export default defineConfig({
  root: "src/web",
  define: { __APP_VERSION__: JSON.stringify(process.env.APP_VERSION || "dev") },
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src/web", import.meta.url)) } },
  build: { outDir: "../../dist/web", emptyOutDir: true },
  server: { proxy: { "/api": "http://localhost:8080" } },
  test: { root: ".", include: ["src/**/*.test.ts"] },
});

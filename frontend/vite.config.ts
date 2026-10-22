import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/trading_companion/web_assets",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: { output: { entryFileNames: "assets/app-[hash].js" } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    restoreMocks: true,
    clearMocks: true,
  },
});

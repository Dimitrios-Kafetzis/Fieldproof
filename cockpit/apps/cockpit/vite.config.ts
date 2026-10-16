import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The application version comes from package.json, which release-please keeps equal to the
// repository version in version.txt.
const pkg = JSON.parse(
  readFileSync(fileURLToPath(new URL("./package.json", import.meta.url)), "utf8"),
) as { version: string };

export default defineConfig({
  plugins: [react()],
  define: {
    __COCKPIT_VERSION__: JSON.stringify(pkg.version),
  },
  server: {
    // Local trust boundary (01-architecture.md): the cockpit binds to localhost only.
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
  },
  build: {
    target: "es2022",
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});

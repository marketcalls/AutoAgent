import { fileURLToPath, URL } from "node:url"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// Tailwind 4 ships a first-party Vite plugin, so there is no tailwind.config.js, no
// postcss.config.js and no autoprefixer: the plugin handles all three.
//
// Ports are deliberately NOT TradingAgent's. That app runs on 5173 in front of 8088;
// this one runs on 5174 in front of 8090 so both can be up at once - which is the
// normal case, since TradingAgent is the tool you reach for when a human is present
// and this one is the tool for the session where nobody is.
//
// strictPort is on because a silent fallback to 5175 would leave the backend's CORS
// allowlist pointing at an origin the browser is no longer using.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) }
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      // Every fetch in the app uses a bare relative /api path so the dev server
      // proxies rather than the browser talking cross-origin. SSE passes through
      // this proxy unbuffered, which is why /api/stream needs no special case.
      "/api": { target: "http://127.0.0.1:8090", changeOrigin: true }
    }
  }
})

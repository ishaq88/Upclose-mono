import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const webRoot = fileURLToPath(new URL(".", import.meta.url));

const DEFAULT_REPO_URL = "https://github.com/ishaq88/Upclose-mono";

/** The public source link shown while the app is not live: HTTPS GitHub only. */
function repoUrl(raw: string | undefined): string {
  const value = raw || DEFAULT_REPO_URL;
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.hostname !== "github.com" || parsed.username || parsed.password || parsed.search || parsed.hash)
    throw new Error("VITE_REPO_URL must be an https://github.com/... repository URL.");
  return parsed.href.replace(/\/$/, "");
}

/** Empty origin = coming-soon mode: product actions point to the sample report and the code instead. */
function appOrigin(raw: string | undefined, allowLocal: boolean): string {
  if (!raw) return "";
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("VITE_APP_ORIGIN must be an absolute origin URL.");
  }
  const loopback = ["localhost", "app.localhost", "127.0.0.1", "[::1]"].includes(
    parsed.hostname,
  );
  if (
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    !(
      parsed.protocol === "https:" ||
      (allowLocal && parsed.protocol === "http:" && loopback)
    )
  ) {
    throw new Error(
      "VITE_APP_ORIGIN must be HTTPS with no credentials, path, query or fragment. HTTP loopback needs explicit LANDING_ALLOW_LOCAL_APP=true for builds.",
    );
  }
  return parsed.origin;
}

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, webRoot, [
    "VITE_APP_ORIGIN",
    "VITE_REPO_URL",
    "LANDING_ALLOW_LOCAL_APP",
  ]);
  const development = command === "serve";
  const raw = process.env.VITE_APP_ORIGIN ?? env.VITE_APP_ORIGIN;
  const allowLocal =
    development ||
    (process.env.LANDING_ALLOW_LOCAL_APP ?? env.LANDING_ALLOW_LOCAL_APP) ===
      "true";
  const origin = appOrigin(raw, allowLocal);
  return {
    root: fileURLToPath(new URL("./marketing", import.meta.url)),
    envDir: webRoot,
    plugins: [react()],
    define: {
      "import.meta.env.VITE_APP_ORIGIN": JSON.stringify(origin),
      "import.meta.env.VITE_REPO_URL": JSON.stringify(repoUrl(process.env.VITE_REPO_URL ?? env.VITE_REPO_URL)),
    },
    server: { host: "127.0.0.1", port: 5180, strictPort: true },
    preview: { host: "127.0.0.1", port: 5180, strictPort: true },
    build: {
      outDir: "../dist-landing",
      emptyOutDir: true,
      rollupOptions: {
        input: {
          main: fileURLToPath(new URL("./marketing/index.html", import.meta.url)),
          sample: fileURLToPath(new URL("./marketing/sample/index.html", import.meta.url)),
        },
      },
    },
  };
});

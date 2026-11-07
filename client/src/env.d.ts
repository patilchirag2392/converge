/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** A ws:// URL, or "same-origin" to use the page's own host. Defaults to
   *  ws://localhost:8000 if unset. See resolveWsBase in useDocSocket.ts. */
  readonly VITE_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

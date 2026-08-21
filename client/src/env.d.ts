/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** e.g. ws://localhost:8000 — defaults to that if unset. */
  readonly VITE_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

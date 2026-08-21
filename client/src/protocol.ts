/**
 * Wire protocol shared with server/app/main.py.
 *
 * Stage 0: the unit of change is the ENTIRE document, not an operation. There is
 * no base revision on the way up, so the server cannot tell a concurrent edit
 * from a sequential one. `rev` is for display only.
 */

export type ServerMessage =
  | {
      type: 'snapshot';
      doc_id: string;
      text: string;
      rev: number;
      clients: number;
      client_id: string;
    }
  | { type: 'edit'; text: string; rev: number; author: string }
  | { type: 'presence'; clients: number }
  | { type: 'error'; detail: string };

export type ClientMessage = { type: 'edit'; text: string };

export type Status = 'connecting' | 'open' | 'closed';

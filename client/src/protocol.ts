/**
 * Wire protocol, shared with server/app/main.py.
 *
 * Stage 1: the unit of change is an *operation naming a character*, never a
 * position and never the whole document. The server merges rather than
 * arbitrates, so `rev` is display-only -- nothing needs an ordering.
 */

import type { Operation } from './crdt';

export type { Operation };

export type ServerMessage =
  | {
      type: 'snapshot';
      doc_id: string;
      ops: Operation[];
      rev: number;
      clients: number;
      client_id: string;
    }
  | { type: 'ops'; ops: Operation[]; rev: number; author: string }
  | { type: 'presence'; clients: number }
  | { type: 'error'; detail: string };

export type ClientMessage = { type: 'ops'; ops: Operation[] };

export type Status = 'connecting' | 'open' | 'closed';

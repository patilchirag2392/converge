import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type RefObject,
} from 'react';
import { RGA, idKey, type Id, type Operation } from './crdt';
import { diffText } from './diff';
import type { ClientMessage, ServerMessage, Status } from './protocol';

const WS_BASE = import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000';
const RECONNECT_DELAY_MS = 1000;
const MAX_EVENTS = 8;

/** One inbound batch of remote operations, kept so the UI can show merges happening. */
export interface RemoteEvent {
  key: number;
  rev: number;
  author: string;
  inserts: number;
  deletes: number;
  at: string;
}

export interface DocSocket {
  text: string;
  rev: number;
  status: Status;
  clients: number;
  clientId: string;
  events: RemoteEvent[];
  /** Operations typed while disconnected, waiting to be sent. */
  queued: number;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  onChange: (event: ChangeEvent<HTMLTextAreaElement>) => void;
}

/** A stable id for this browser tab. sessionStorage is per-tab, so two tabs on
 *  the same URL are genuinely different replicas. */
function useClientId(): string {
  const [clientId] = useState(() => {
    const existing = sessionStorage.getItem('converge:client_id');
    if (existing) return existing;
    const fresh = crypto.randomUUID().slice(0, 8);
    sessionStorage.setItem('converge:client_id', fresh);
    return fresh;
  });
  return clientId;
}

/**
 * Where the caret should land after remote operations are merged.
 *
 * Offsets are meaningless across a merge -- a remote insert above you shifts
 * every offset below it. So we anchor to a *character identity* instead: the
 * nearest character still visible to the left of the caret, walking left in case
 * the one we were sitting after was deleted by the same batch. Exactly the trick
 * the CRDT uses for text, applied to the cursor.
 */
function remapCaret(idsAfter: Id[], idsBefore: Id[], caretBefore: number): number {
  const position = new Map<string, number>();
  idsAfter.forEach((id, index) => position.set(idKey(id), index));

  for (let i = Math.min(caretBefore, idsBefore.length) - 1; i >= 0; i -= 1) {
    const index = position.get(idKey(idsBefore[i]));
    if (index !== undefined) return index + 1;
  }
  return 0;
}

export function useDocSocket(docId: string): DocSocket {
  const clientId = useClientId();
  const [text, setText] = useState('');
  const [rev, setRev] = useState(0);
  const [status, setStatus] = useState<Status>('connecting');
  const [clients, setClients] = useState(0);
  const [events, setEvents] = useState<RemoteEvent[]>([]);
  const [queued, setQueued] = useState(0);

  const socketRef = useRef<WebSocket | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const caretRef = useRef<number | null>(null);
  const outboxRef = useRef<Operation[]>([]);

  // The replica lives in a ref: it must survive re-renders, and it must survive
  // a reconnect (it may hold edits the server hasn't seen yet).
  const crdtRef = useRef<RGA | null>(null);
  const crdt = (): RGA => {
    if (crdtRef.current === null) crdtRef.current = new RGA(clientId);
    return crdtRef.current;
  };

  const flush = useCallback(() => {
    const socket = socketRef.current;
    if (socket?.readyState !== WebSocket.OPEN || outboxRef.current.length === 0) return;
    const message: ClientMessage = { type: 'ops', ops: outboxRef.current };
    socket.send(JSON.stringify(message));
    outboxRef.current = [];
    setQueued(0);
  }, []);

  /**
   * Re-send everything this replica knows, after reconnecting.
   *
   * The hole this closes: if the server died after we applied an edit locally
   * but before its database write committed, that operation exists only in this
   * browser. The server's snapshot cannot contain it, and merging cannot invent
   * it -- so reconnect has to be a *mutual* exchange. Both sides send what they
   * have; both merge.
   *
   * Only sane because operations are idempotent and commutative: the server
   * drops everything it already knows and relays only the genuine gaps, so this
   * is cheap for everyone except the uploader. In Stage 0 the same move would
   * have clobbered every other tab with a stale whole-document write.
   */
  const replayLocalState = useCallback(() => {
    const socket = socketRef.current;
    if (socket?.readyState !== WebSocket.OPEN) return;
    const message: ClientMessage = { type: 'ops', ops: crdt().snapshotOps() };
    socket.send(JSON.stringify(message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const queue = useCallback(
    (ops: Operation[]) => {
      // Safe to sit on these: operations commute and are idempotent, so sending
      // them late merges exactly as well as sending them now. That's what makes
      // editing while disconnected work at all -- impossible in Stage 0, where a
      // stale whole-document write would have clobbered everyone.
      outboxRef.current.push(...ops);
      setQueued(outboxRef.current.length);
      flush();
    },
    [flush],
  );

  const merge = useCallback((ops: Operation[]) => {
    const replica = crdt();
    const element = textareaRef.current;
    const focused = element !== null && document.activeElement === element;
    const caretBefore = focused ? element.selectionStart : null;
    const idsBefore = caretBefore !== null && caretBefore > 0 ? replica.visibleIds() : null;

    for (const op of ops) {
      try {
        replica.apply(op);
      } catch (error) {
        console.warn('[converge] dropping unusable operation', op, error);
      }
    }

    setText(replica.text);
    setRev(replica.applied);

    if (caretBefore !== null) {
      caretRef.current =
        idsBefore === null ? 0 : remapCaret(replica.visibleIds(), idsBefore, caretBefore);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Restore the caret after React has committed the merged text. Collapses a
  // selection to a caret, which is a real (small) limitation.
  useLayoutEffect(() => {
    const element = textareaRef.current;
    const target = caretRef.current;
    if (element === null || target === null) return;
    caretRef.current = null;
    element.setSelectionRange(target, target);
  }, [text]);

  useEffect(() => {
    let disposed = false;
    let reconnectTimer: number | undefined;
    let socket: WebSocket;

    const connect = () => {
      setStatus('connecting');
      socket = new WebSocket(`${WS_BASE}/ws/${encodeURIComponent(docId)}?client_id=${clientId}`);
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) return;
        setStatus('open');
        flush();
      };

      socket.onmessage = (event) => {
        if (disposed) return;
        let message: ServerMessage;
        try {
          message = JSON.parse(event.data as string) as ServerMessage;
        } catch {
          console.warn('[converge] unparseable frame from server:', event.data);
          return;
        }

        switch (message.type) {
          case 'snapshot': {
            // Anything already here is state the server may not know about --
            // this is a reconnect, not a first load.
            const hadLocalState = crdt().applied > 0;

            // Merge rather than replace. On a reconnect this replica may already
            // hold edits the server has never seen, and re-applying operations
            // we already have is a no-op, so merging is always correct.
            merge(message.ops);
            setClients(message.clients);
            flush();

            if (hadLocalState) replayLocalState();
            break;
          }

          case 'ops':
            merge(message.ops);
            setEvents((previous) =>
              [
                {
                  key: message.rev,
                  rev: message.rev,
                  author: message.author,
                  inserts: message.ops.filter((op) => op.kind === 'insert').length,
                  deletes: message.ops.filter((op) => op.kind === 'delete').length,
                  at: new Date().toLocaleTimeString(),
                },
                ...previous,
              ].slice(0, MAX_EVENTS),
            );
            break;

          case 'presence':
            setClients(message.clients);
            break;

          case 'error':
            console.warn('[converge] server rejected a frame:', message.detail);
            break;
        }
      };

      socket.onclose = () => {
        if (disposed) return;
        setStatus('closed');
        setClients(0);
        reconnectTimer = window.setTimeout(connect, RECONNECT_DELAY_MS);
      };
    };

    connect();

    return () => {
      // StrictMode mounts effects twice in dev, so this has to be exact or you
      // end up with two live sockets per tab.
      disposed = true;
      window.clearTimeout(reconnectTimer);
      socketRef.current = null;
      socket.close();
    };
  }, [docId, clientId, merge, flush, replayLocalState]);

  const onChange = useCallback(
    (event: ChangeEvent<HTMLTextAreaElement>) => {
      const replica = crdt();
      const change = diffText(replica.text, event.target.value);
      if (change === null) return;

      // Delete first, then insert at the same offset: after the removal, the
      // character to the left of `index` is the one the insert should follow.
      const ops: Operation[] = [];
      if (change.removed > 0) ops.push(...replica.localDelete(change.index, change.removed));
      if (change.inserted.length > 0) {
        ops.push(...replica.localInsert(change.index, change.inserted));
      }

      // No caret fixup needed here: the browser already placed it, and the text
      // we render is identical to what the user just typed.
      setText(replica.text);
      setRev(replica.applied);
      queue(ops);
      // eslint-disable-next-line react-hooks/exhaustive-deps
    },
    [queue],
  );

  return { text, rev, status, clients, clientId, events, queued, textareaRef, onChange };
}

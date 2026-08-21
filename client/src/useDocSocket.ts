import { useCallback, useEffect, useRef, useState } from 'react';
import type { ClientMessage, ServerMessage, Status } from './protocol';

const WS_BASE = import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000';
const RECONNECT_DELAY_MS = 1000;
const MAX_EVENTS = 8;

/** One inbound remote edit, kept only so the UI can show the clobbering happen. */
export interface RemoteEvent {
  key: number;
  rev: number;
  author: string;
  text: string;
  at: string;
}

export interface DocSocket {
  text: string;
  rev: number;
  status: Status;
  clients: number;
  clientId: string;
  events: RemoteEvent[];
  setLocalText: (text: string) => void;
}

/** A stable id for this browser tab. sessionStorage is per-tab, so two tabs on
 *  the same URL get different ids and the server can skip echoing to the author. */
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

export function useDocSocket(docId: string): DocSocket {
  const clientId = useClientId();
  const [text, setText] = useState('');
  const [rev, setRev] = useState(0);
  const [status, setStatus] = useState<Status>('connecting');
  const [clients, setClients] = useState(0);
  const [events, setEvents] = useState<RemoteEvent[]>([]);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let disposed = false;
    let reconnectTimer: number | undefined;
    let socket: WebSocket;

    const connect = () => {
      setStatus('connecting');
      const url = `${WS_BASE}/ws/${encodeURIComponent(docId)}?client_id=${clientId}`;
      socket = new WebSocket(url);
      socketRef.current = socket;

      socket.onopen = () => {
        if (!disposed) setStatus('open');
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
          case 'snapshot':
            setText(message.text);
            setRev(message.rev);
            setClients(message.clients);
            break;

          case 'edit':
            // Here is the damage. We replace the entire local document with the
            // other tab's version, so anything typed here since our last send is
            // silently destroyed. The caret also jumps to the end, because the
            // <textarea> value changed wholesale rather than being patched.
            setText(message.text);
            setRev(message.rev);
            setEvents((previous) =>
              [
                {
                  key: message.rev,
                  rev: message.rev,
                  author: message.author,
                  text: message.text,
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
      // StrictMode mounts effects twice in dev, so this has to be exact or
      // you end up with two live sockets per tab.
      disposed = true;
      window.clearTimeout(reconnectTimer);
      socketRef.current = null;
      socket.close();
    };
  }, [docId, clientId]);

  const setLocalText = useCallback((next: string) => {
    // Local state wins immediately, so typing never waits on the network.
    setText(next);

    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) {
      const message: ClientMessage = { type: 'edit', text: next };
      socket.send(JSON.stringify(message));
    }
    // Deliberately absent: debouncing (it would shrink the race window and hide
    // the bug), and any queue/retry for keystrokes typed while disconnected
    // (they are simply lost).
  }, []);

  return { text, rev, status, clients, clientId, events, setLocalText };
}

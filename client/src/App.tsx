import { useDocSocket } from './useDocSocket';

/** Doc id comes from the query string so two tabs on the same URL share a room. */
function readDocId(): string {
  return new URLSearchParams(window.location.search).get('doc')?.trim() || 'demo';
}

const DOC_ID = readDocId();

export default function App() {
  const { text, rev, status, clients, clientId, events, setLocalText } = useDocSocket(DOC_ID);

  return (
    <div className="app">
      <header className="bar">
        <div className="brand">
          <strong>Converge</strong>
          <span className="stage">Stage 0 · last-write-wins</span>
        </div>
        <div className="meta">
          <span className={`pill status-${status}`}>{status}</span>
          <span className="pill">doc: {DOC_ID}</span>
          <span className="pill">tab: {clientId}</span>
          <span className="pill">clients: {clients}</span>
          <span className="pill">rev: {rev}</span>
        </div>
      </header>

      <main className="editor-wrap">
        <textarea
          className="editor"
          value={text}
          onChange={(event) => setLocalText(event.target.value)}
          placeholder="Type here. Then open a second tab on the same URL and type in both at once."
          spellCheck={false}
        />
        <div className="under">
          <span>{text.length} chars — the whole document is sent on every keystroke</span>
          <button type="button" onClick={() => window.open(window.location.href, '_blank')}>
            Open a second tab
          </button>
        </div>
      </main>

      <aside className="log">
        <h2>Inbound remote edits</h2>
        <p className="hint">
          Each one <em>replaced</em> your entire document. If you were mid-word, that word is gone
          and it is not recoverable — the server never stored it.
        </p>
        {events.length === 0 ? (
          <p className="empty">Nothing yet. Type in the other tab.</p>
        ) : (
          <ol>
            {events.map((event) => (
              <li key={event.key}>
                <code>rev {event.rev}</code> from <code>{event.author}</code>
                <span className="at">{event.at}</span>
                <div className="preview">{event.text.slice(0, 120) || '(empty)'}</div>
              </li>
            ))}
          </ol>
        )}
      </aside>
    </div>
  );
}

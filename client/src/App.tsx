import { useDocSocket } from './useDocSocket';

/** Doc id comes from the query string so two tabs on the same URL share a room. */
function readDocId(): string {
  return new URLSearchParams(window.location.search).get('doc')?.trim() || 'demo';
}

const DOC_ID = readDocId();

export default function App() {
  const { text, rev, status, clients, clientId, events, queued, textareaRef, onChange } =
    useDocSocket(DOC_ID);

  return (
    <div className="app">
      <header className="bar">
        <div className="brand">
          <strong>Converge</strong>
          <span className="stage">Stage 1 · CRDT</span>
        </div>
        <div className="meta">
          <span className={`pill status-${status}`}>{status}</span>
          <span className="pill">doc: {DOC_ID}</span>
          <span className="pill">tab: {clientId}</span>
          <span className="pill">clients: {clients}</span>
          <span className="pill">ops: {rev}</span>
          {queued > 0 && <span className="pill queued">queued: {queued}</span>}
        </div>
      </header>

      <main className="editor-wrap">
        <textarea
          ref={textareaRef}
          className="editor"
          value={text}
          onChange={onChange}
          placeholder="Type here. Then open a second tab on the same URL and type in both at once — both sets of keystrokes survive."
          spellCheck={false}
        />
        <div className="under">
          <span>
            {text.length} chars — only the characters you changed go over the wire, as operations
          </span>
          <button type="button" onClick={() => window.open(window.location.href, '_blank')}>
            Open a second tab
          </button>
        </div>
      </main>

      <aside className="log">
        <h2>Merged remote operations</h2>
        <p className="hint">
          Each batch was <em>merged</em> into your document rather than replacing it. Nothing you
          typed was lost, and your cursor stayed where you left it.
        </p>
        {events.length === 0 ? (
          <p className="empty">Nothing yet. Type in the other tab.</p>
        ) : (
          <ol>
            {events.map((event) => (
              <li key={event.key}>
                <code>op {event.rev}</code> from <code>{event.author}</code>
                <span className="at">{event.at}</span>
                <div className="preview">
                  {event.inserts > 0 && <span className="ins">+{event.inserts} inserted</span>}
                  {event.inserts > 0 && event.deletes > 0 && ' · '}
                  {event.deletes > 0 && <span className="del">−{event.deletes} deleted</span>}
                </div>
              </li>
            ))}
          </ol>
        )}
      </aside>
    </div>
  );
}

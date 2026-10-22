import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { API_BASE, checkHealth, fetchMeta, sendQuestion } from './api.js';

/* ------------------------------------------------------------------------ *
 * Markdown renderer
 *
 * Safety model:
 *   - react-markdown does NOT enable rehype-raw, so raw HTML in the payload
 *     is never parsed as markup.
 *   - `skipHtml` removes any HTML nodes outright (defence in depth).
 *   - `urlTransform` uses react-markdown's default allow-list, which strips
 *     `javascript:` / `data:` URLs from links and images.
 *   - remark-gfm adds table/strikethrough/autolink support. It cannot emit
 *     script or event handlers.
 * ------------------------------------------------------------------------ */
function Markdown({ children }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          p: ({ node, ...props }) => <p {...props} />,
          a: ({ node, ...props }) => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
          ul: ({ node, ...props }) => <ul {...props} />,
          ol: ({ node, ...props }) => <ol {...props} />,
          li: ({ node, ...props }) => <li {...props} />,
          strong: ({ node, ...props }) => <strong {...props} />,
          em: ({ node, ...props }) => <em {...props} />,
          hr: ({ node, ...props }) => <hr {...props} />,
          blockquote: ({ node, ...props }) => <blockquote {...props} />,
          code: ({ node, className, ...props }) => (
            <code className={className || 'inline-code'} {...props} />
          ),
          pre: ({ node, ...props }) => <pre className="code-pre" {...props} />,
          table: ({ node, ...props }) => (
            <div className="table-scroll">
              <table {...props} />
            </div>
          ),
          thead: ({ node, ...props }) => <thead {...props} />,
          tbody: ({ node, ...props }) => <tbody {...props} />,
          tr: ({ node, ...props }) => <tr {...props} />,
          th: ({ node, ...props }) => <th {...props} />,
          td: ({ node, ...props }) => <td {...props} />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

export { Markdown };

/* ------------------------------------------------------------------------ *
 * Typing indicator
 * ------------------------------------------------------------------------ */
function TypingIndicator() {
  return (
    <div className="message assistant">
      <div className="avatar" aria-hidden="true">
        P1
      </div>
      <div className="bubble typing-bubble" role="status" aria-live="polite">
        <span className="typing-label">Analysing</span>
        <span className="dots">
          <span className="dot" />
          <span className="dot" />
          <span className="dot" />
        </span>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ *
 * Message card
 * ------------------------------------------------------------------------ */
function MessageCard({ message, onSuggestion }) {
  const isUser = message.role === 'user';
  const meta = message.meta || {};

  return (
    <div className={`message ${isUser ? 'user' : 'assistant'}`}>
      {!isUser && (
        <div className="avatar" aria-hidden="true">
          P1
        </div>
      )}
      <div className="bubble">
        {isUser ? (
          <p className="user-text">{message.content}</p>
        ) : (
          <>
            <Markdown>{message.content}</Markdown>

            {meta.status === 'fallback' && (
              <div className="badge badge-warn">No matching analysis</div>
            )}
            {meta.status === 'ambiguous' && (
              <div className="badge badge-warn">Ambiguous — please pick one</div>
            )}
            {meta.status === 'error' && <div className="badge badge-error">Query failed</div>}

            {meta.intent && (
              <div className="meta-row">
                <span className="badge">{meta.intent}</span>
                {typeof meta.confidence === 'number' && (
                  <span className="confidence" title="Cosine similarity to the intent">
                    {(meta.confidence * 100).toFixed(0)}% match
                  </span>
                )}
                {typeof meta.rowCount === 'number' && meta.rowCount > 0 && (
                  <span className="confidence">{meta.rowCount} rows</span>
                )}
              </div>
            )}

            {meta.sql && (
              <details className="sql-panel">
                <summary>SQL executed</summary>
                <pre className="code-pre">{meta.sql}</pre>
              </details>
            )}

            {meta.suggestions?.length > 0 && (
              <div className="chips">
                {meta.suggestions.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="chip"
                    onClick={() => onSuggestion(s)}
                    disabled={message.busy}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ *
 * App
 * ------------------------------------------------------------------------ */
export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [meta, setMeta] = useState(null);
  const [status, setStatus] = useState('connecting'); // connecting | online | offline
  const [error, setError] = useState(null);

  const scrollRef = useRef(null);
  const inputRef = useRef(null);
  const idRef = useRef(0);

  /* -- bootstrap --------------------------------------------------------- */
  useEffect(() => {
    let cancelled = false;

    async function boot() {
      try {
        const health = await checkHealth();
        if (cancelled) return;
        setStatus(health.status === 'ok' ? 'online' : 'connecting');
        setError(null);
      } catch {
        if (!cancelled) setStatus('offline');
      }
      try {
        const m = await fetchMeta();
        if (!cancelled) setMeta(m);
      } catch {
        /* meta is optional chrome; the chat still works without it */
      }
    }

    boot();
    const timer = setInterval(boot, 30000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  /* -- keep the transcript pinned to the bottom -------------------------- */
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, busy]);

  /* -- core ask ---------------------------------------------------------- */
  const ask = useCallback(async (question) => {
    const trimmed = question.trim();
    if (!trimmed) return;

    const userMsg = { id: ++idRef.current, role: 'user', content: trimmed };
    setMessages((prev) => [...prev, userMsg]);
    setInput('');
    setBusy(true);
    setError(null);

    try {
      const res = await sendQuestion(trimmed);
      setMessages((prev) => [
        ...prev,
        {
          id: ++idRef.current,
          role: 'assistant',
          content: res.answer,
          meta: {
            status: res.status,
            intent: res.intent,
            confidence: res.confidence,
            sql: res.sql,
            suggestions: res.suggestions,
            rowCount: res.row_count,
          },
        },
      ]);
      setStatus('online');
    } catch (err) {
      setStatus('offline');
      setError(err.message || 'Request failed');
      setMessages((prev) => [
        ...prev,
        {
          id: ++idRef.current,
          role: 'assistant',
          content:
            `**Could not reach the analytics API.**\n\n` +
            `${err.message}\n\n` +
            `Make sure the backend is running on \`${API_BASE}\` \`\`\`\n` +
            `cd backend\n.\\.venv\\Scripts\\Activate.ps1\nuvicorn main:app --reload --port 8000\n\`\`\``,
          meta: { status: 'error' },
        },
      ]);
    } finally {
      setBusy(false);
      inputRef.current?.focus();
    }
  }, []);

  const onSubmit = (event) => {
    event.preventDefault();
    ask(input);
  };

  /* -- deep link: /?q=<question> asks automatically on load ---------------- */
  const autoAskedRef = useRef(false);
  useEffect(() => {
    if (autoAskedRef.current) return;
    const q = new URLSearchParams(window.location.search).get('q');
    if (!q || !q.trim()) return;
    autoAskedRef.current = true;
    ask(q);
  }, [ask]);

  const onKeyDown = (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      ask(input);
    }
  };

  const starterChips = useMemo(
    () => [
      'What is our total revenue?',
      'Show me the monthly revenue trend',
      'Revenue by region',
      'What are our top selling products?',
      'Which sales channel performs best?',
      'Are we profitable?',
      'Who are our top customers?',
      'How many orders were returned?',
    ],
    [],
  );

  const statusLabel = {
    connecting: 'connecting…',
    online: 'API online',
    offline: 'API offline',
  }[status];

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">P1</div>
          <div>
            <h1>Local Analytics</h1>
            <p className="brand-sub">
              Semantic intent routing · DuckDB · no LLM
            </p>
          </div>
        </div>

        <div className="topbar-meta">
          {meta?.data_range?.start && (
            <span className="pill">
              data {meta.data_range.start} → {meta.data_range.end}
            </span>
          )}
          {meta?.row_counts?.sales && (
            <span className="pill">{meta.row_counts.sales.toLocaleString()} orders</span>
          )}
          <span className={`status status-${status}`}>
            <span className="status-dot" />
            {statusLabel}
          </span>
        </div>
      </header>

      {error && status === 'offline' && (
        <div className="banner banner-error">
          <strong>Backend unreachable.</strong> Start it with{' '}
          <code className="inline-code">uvicorn main:app --reload --port 8000</code> from
          the <code className="inline-code">backend</code> folder.
        </div>
      )}

      <main className="chat-scroll" ref={scrollRef}>
        <div className="chat-column">
          {messages.length === 0 && (
            <section className="welcome">
              <h2>Ask the warehouse a question.</h2>
              <p>
                Your question is embedded locally with{' '}
                <code className="inline-code">all-MiniLM-L6-v2</code> and matched against a
                fixed catalogue of analytical intents. Only questions that map cleanly to a
                known scope are executed.
              </p>
              <div className="chips chips-lg">
                {starterChips.map((c) => (
                  <button key={c} type="button" className="chip" onClick={() => ask(c)}>
                    {c}
                  </button>
                ))}
              </div>
            </section>
          )}

          {messages.map((m) => (
            <MessageCard key={m.id} message={m} onSuggestion={ask} />
          ))}

          {busy && <TypingIndicator />}
        </div>
      </main>

      <footer className="composer">
        <form className="composer-inner" onSubmit={onSubmit}>
          <textarea
            ref={inputRef}
            className="textarea"
            rows={1}
            value={input}
            placeholder="Ask about revenue, profitability, regions, channels, customers…"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={busy}
            aria-label="Your question"
          />
          <button
            type="submit"
            className="send-btn"
            disabled={busy || !input.trim()}
            aria-label="Send"
          >
            {busy ? <span className="spinner" /> : <span className="send-icon">→</span>}
          </button>
        </form>
        <p className="hint">
          <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
        </p>
      </footer>
    </div>
  );
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import Chart, { ChartLegend, formatValue } from './Chart.jsx';
import {
  activateDataset,
  checkHealth,
  deleteDataset,
  fetchInsights,
  fetchMeta,
  fetchSchema,
  listDatasets,
  sendQuestion,
  uploadDataset,
} from './api.js';

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
 * Small pieces
 * ------------------------------------------------------------------------ */

const FILE_ACCEPT = '.csv,.tsv,.txt,.json,.jsonl,.ndjson,.xlsx,.xlsm';

const STATUS_COPY = {
  ok: 'Answered',
  ambiguous: 'Two readings',
  fallback: 'Out of scope',
  needs_column: 'Unknown column',
  needs_dataset: 'No data yet',
  loading: 'Still warming up',
  error: 'Request failed',
};

const ROLE_COPY = {
  metric: 'measure',
  dimension: 'category',
  time: 'date',
  entity: 'name',
};

function uid() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function Icon({ name, size = 16 }) {
  const paths = {
    plus: 'M8 3v10M3 8h10',
    send: 'M3 8l10-5-3.2 10L8 9 3 8z',
    file: 'M4 2h5l3 3v9H4V2zm5 0v3h3',
    chevron: 'M6 4l4 4-4 4',
    alert: 'M8 2l6 11H2L8 2zm0 4v4m0 2.2v.1',
    check: 'M3 8.5l3 3L13 5',
    spark: 'M8 2v3M8 11v3M2 8h3M11 8h3M4 4l2 2M10 10l2 2M12 4l-2 2M6 10l-2 2',
    x: 'M4 4l8 8M12 4l-8 8',
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={paths[name] || paths.file} />
    </svg>
  );
}

function Stat({ label, value, hint }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  );
}

/* ------------------------------------------------------------------------ *
 * Upload
 * ------------------------------------------------------------------------ */

function UploadButton({ onFiles, busy, variant = 'primary', label = 'Add data' }) {
  const inputRef = useRef(null);

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept={FILE_ACCEPT}
        multiple
        className="sr-only"
        onChange={(event) => {
          onFiles(event.target.files);
          event.target.value = '';
        }}
      />
      <button
        type="button"
        className={`btn btn-${variant}`}
        onClick={() => inputRef.current?.click()}
        disabled={busy}
      >
        <Icon name="plus" />
        {label}
      </button>
    </>
  );
}

function DropZone({ onFiles, busy }) {
  const inputRef = useRef(null);
  const [over, setOver] = useState(false);

  return (
    <div
      className={`dropzone ${over ? 'is-over' : ''}`}
      onDragOver={(event) => {
        event.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setOver(false);
        onFiles(event.dataTransfer.files);
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept={FILE_ACCEPT}
        multiple
        className="sr-only"
        onChange={(event) => {
          onFiles(event.target.files);
          event.target.value = '';
        }}
      />
      <div className="dropzone-mark" aria-hidden="true">
        <Icon name="file" size={22} />
      </div>
      <h2>Bring your own data</h2>
      <p>
        Drop a spreadsheet or a CSV export here. P1 reads the columns, works out which ones are
        dates, categories and measures, then draws the first charts for you.
      </p>
      <div className="dropzone-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => inputRef.current?.click()}
          disabled={busy}
        >
          Choose files
        </button>
        <span className="dropzone-hint">CSV, TSV, JSON, Excel · up to 64 MB</span>
      </div>
    </div>
  );
}

function UploadProgress({ upload }) {
  if (!upload) return null;
  const percent = Math.round((upload.progress || 0) * 100);
  return (
    <div className="upload-progress" role="status" aria-live="polite">
      <div className="upload-progress-head">
        <span>
          Reading {upload.count > 1 ? `${upload.count} files` : upload.name}
        </span>
        <span className="tabular">{percent}%</span>
      </div>
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${Math.max(4, percent)}%` }} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ *
 * Rail
 * ------------------------------------------------------------------------ */

function Rail({ datasets, schema, suggestions, busy, onActivate, onRemove, onAsk }) {
  const [confirming, setConfirming] = useState(null);
  const [showAll, setShowAll] = useState(false);

  const columns = schema?.tables?.flatMap((table) =>
    table.columns.map((column) => ({ ...column, table: table.name })),
  ) || [];
  const visible = showAll ? columns : columns.slice(0, 8);

  return (
    <aside className="rail" aria-label="Dataset details">
      <section className="rail-section">
        <h2 className="rail-title">Datasets</h2>
        <ul className="dataset-list">
          {datasets.map((dataset) => (
            <li key={dataset.id}>
              <div className={`dataset-row ${dataset.active ? 'is-active' : ''}`}>
                <button
                  type="button"
                  className="dataset-pick"
                  onClick={() => !dataset.active && onActivate(dataset.id)}
                  aria-current={dataset.active ? 'true' : undefined}
                >
                  <span className="dataset-name">{dataset.name}</span>
                  <span className="dataset-meta tabular">
                    {dataset.row_count.toLocaleString()} rows · {dataset.column_count} columns
                  </span>
                </button>
                {confirming === dataset.id ? (
                  <span className="confirm">
                    <button
                      type="button"
                      className="link-danger"
                      onClick={() => {
                        setConfirming(null);
                        onRemove(dataset.id);
                      }}
                    >
                      Remove
                    </button>
                    <button
                      type="button"
                      className="link-muted"
                      onClick={() => setConfirming(null)}
                    >
                      Keep
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="icon-btn"
                    aria-label={`Remove ${dataset.name}`}
                    onClick={() => setConfirming(dataset.id)}
                  >
                    <Icon name="x" size={13} />
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>

      {columns.length > 0 && (
        <section className="rail-section">
          <h2 className="rail-title">What P1 understood</h2>
          <ul className="column-list">
            {visible.map((column) => (
              <li key={`${column.table}.${column.name}`}>
                <span className="column-name" title={column.name}>
                  {column.label}
                </span>
                <span className={`role role-${column.role}`}>
                  {ROLE_COPY[column.role] || column.role}
                </span>
              </li>
            ))}
          </ul>
          {columns.length > visible.length && (
            <button type="button" className="link-muted more" onClick={() => setShowAll(true)}>
              {columns.length - visible.length} more columns
            </button>
          )}
        </section>
      )}

      {suggestions.length > 0 && (
        <section className="rail-section">
          <h2 className="rail-title">Try asking</h2>
          <ul className="suggest-list">
            {suggestions.slice(0, 5).map((suggestion) => (
              <li key={suggestion}>
                <button
                  type="button"
                  className="suggest"
                  onClick={() => onAsk(suggestion)}
                  disabled={busy}
                >
                  {suggestion}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </aside>
  );
}

/* ------------------------------------------------------------------------ *
 * Insights
 * ------------------------------------------------------------------------ */

function InsightBoard({ insights }) {
  const charts = insights?.charts || [];
  const summary = insights?.summary;
  if (!summary) return null;

  const total = formatValue(summary.metric_total ?? 0, summary.metric_unit);
  const average = formatValue(summary.metric_average ?? 0, summary.metric_unit);

  return (
    <section className="board" aria-labelledby="board-heading">
      <div className="board-head">
        <h2 id="board-heading">First look</h2>
        <p className="board-sub">
          Generated from your columns the moment the upload finished. Ask a question below for
          anything more specific.
        </p>
      </div>

      <div className="stats">
        <Stat label="Rows" value={summary.row_count.toLocaleString()} />
        <Stat label="Columns" value={String(summary.column_count)} />
        <Stat label={summary.primary_metric || 'Total'} value={total} hint={`average ${average}`} />
        <Stat
          label="Date range"
          value={summary.date_range ? summary.date_range.replace(' → ', ' – ') : 'No dates'}
        />
      </div>

      {charts.length > 0 && (
        <div className="chart-grid">
          {charts.map((chart) => (
            <div className="chart-card" key={chart.id}>
              <Chart spec={chart} />
              {chart.series?.length > 0 && <ChartLegend series={chart.series} />}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

/* ------------------------------------------------------------------------ *
 * Conversation
 * ------------------------------------------------------------------------ */

function ReplyMeta({ message }) {
  const status = message.status || 'ok';
  const pieces = [];

  if (message.analysis_description) pieces.push(message.analysis_description);
  if (typeof message.confidence === 'number' && message.confidence > 0) {
    pieces.push(`${Math.round(message.confidence * 100)}% match`);
  }
  if (message.row_count > 0) pieces.push(`${message.row_count.toLocaleString()} rows`);

  return (
    <div className="reply-meta">
      <span className={`pill pill-${status}`}>{STATUS_COPY[status] || status}</span>
      {pieces.length > 0 && <span className="reply-facts">{pieces.join(' · ')}</span>}
    </div>
  );
}

function MessageCard({ message, onAsk, busy }) {
  if (message.role === 'user') {
    return (
      <div className="turn turn-user">
        <p className="ask-text">{message.content}</p>
      </div>
    );
  }

  return (
    <article className="turn turn-reply">
      <ReplyMeta message={message} />
      <Markdown>{message.answer || ''}</Markdown>

      {message.chart && (
        <div className="reply-chart">
          <Chart spec={message.chart} compact />
          {message.chart.series?.length > 0 && <ChartLegend series={message.chart.series} />}
        </div>
      )}

      {message.sql && (
        <details className="sql">
          <summary>SQL behind this answer</summary>
          <pre className="code-pre">{message.sql}</pre>
        </details>
      )}

      {message.suggestions?.length > 0 && (
        <div className="follow-ups">
          {message.suggestions.slice(0, 4).map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              className="chip"
              onClick={() => onAsk(suggestion)}
              disabled={busy}
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}
    </article>
  );
}

function Thinking() {
  return (
    <div className="turn turn-reply turn-thinking" role="status" aria-live="polite">
      <span className="thinking-label">Working through the data</span>
      <span className="thinking-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
    </div>
  );
}

/* ------------------------------------------------------------------------ *
 * Composer
 * ------------------------------------------------------------------------ */

function Composer({ value, onChange, onSend, busy, disabled, placeholder }) {
  const areaRef = useRef(null);

  useEffect(() => {
    const element = areaRef.current;
    if (!element) return;
    element.style.height = 'auto';
    element.style.height = `${Math.min(160, element.scrollHeight)}px`;
  }, [value]);

  const submit = () => {
    if (!value.trim() || busy || disabled) return;
    onSend(value);
  };

  return (
    <div className="composer">
      <div className="composer-box">
        <label className="sr-only" htmlFor="question">
          Ask a question about your data
        </label>
        <textarea
          id="question"
          ref={areaRef}
          rows={1}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              submit();
            }
          }}
        />
        <button
          type="button"
          className="btn btn-send"
          onClick={submit}
          disabled={busy || disabled || !value.trim()}
          aria-label="Send question"
        >
          <Icon name="send" />
        </button>
      </div>
      <p className="composer-note">
        Runs entirely on this machine. Enter to send, Shift + Enter for a new line.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------------------ *
 * App
 * ------------------------------------------------------------------------ */

export default function App() {
  const [health, setHealth] = useState(null);
  const [datasets, setDatasets] = useState([]);
  const [meta, setMeta] = useState(null);
  const [insights, setInsights] = useState(null);
  const [schema, setSchema] = useState(null);
  const [messages, setMessages] = useState([]);
  const [busy, setBusy] = useState(false);
  const [upload, setUpload] = useState(null);
  const [notice, setNotice] = useState(null);
  const [draft, setDraft] = useState('');
  const [booting, setBooting] = useState(true);

  const streamRef = useRef(null);
  const deepLink = useRef(false);

  const active = useMemo(
    () => datasets.find((dataset) => dataset.active) || datasets[0] || null,
    [datasets],
  );

  const suggestions = useMemo(() => meta?.suggestions || [], [meta]);
  const ready = health?.model_state === 'ready';
  const offline = health?.status === 'offline';

  /* -- load everything ---------------------------------------------------- */

  const loadData = useCallback(async () => {
    const listing = await listDatasets();
    const nextDatasets = listing.datasets || [];
    setDatasets(nextDatasets);

    if (nextDatasets.length === 0) {
      setInsights(null);
      setSchema(null);
      return;
    }

    const [nextInsights, nextSchema] = await Promise.all([fetchInsights(), fetchSchema()]);
    setInsights(nextInsights);
    setSchema(nextSchema);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer;

    const poll = async () => {
      try {
        const next = await checkHealth();
        if (cancelled) return;
        setHealth(next);

        if (next.model_state === 'ready') {
          try {
            const metaData = await fetchMeta();
            if (cancelled) return;
            setMeta(metaData);
            await loadData();
          } catch (error) {
            if (!cancelled) setNotice({ kind: 'error', text: error.message });
          } finally {
            if (!cancelled) setBooting(false);
          }
          return;
        }
        timer = setTimeout(poll, 2000);
      } catch (error) {
        if (cancelled) return;
        setHealth({ status: 'offline', error: error.message });
        setBooting(false);
        timer = setTimeout(poll, 3000);
      }
    };

    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [loadData]);

  /* -- keep the transcript in view ---------------------------------------- */

  useEffect(() => {
    const element = streamRef.current;
    if (element) element.scrollTo({ top: element.scrollHeight, behavior: 'smooth' });
  }, [messages.length, busy]);

  /* -- ask ---------------------------------------------------------------- */

  const ask = useCallback(
    async (question) => {
      const text = String(question || '').trim();
      if (!text) return;

      setDraft('');
      setNotice(null);
      setMessages((previous) => [...previous, { id: uid(), role: 'user', content: text }]);
      setBusy(true);

      try {
        const reply = await sendQuestion(text);
        setMessages((previous) => [...previous, { id: uid(), role: 'assistant', ...reply }]);
      } catch (error) {
        setMessages((previous) => [
          ...previous,
          {
            id: uid(),
            role: 'assistant',
            status: 'error',
            answer: `**The analytics service did not answer.**\n\n${error.message}`,
          },
        ]);
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  /* -- deep link: /?q=revenue%20by%20region -------------------------------- */

  useEffect(() => {
    if (deepLink.current || !ready || !active || booting) return;
    const question = new URLSearchParams(window.location.search).get('q');
    if (!question) return;
    deepLink.current = true;
    ask(question);
  }, [ready, active, booting, ask]);

  /* -- upload ------------------------------------------------------------- */

  const handleFiles = useCallback(
    async (fileList) => {
      const files = Array.from(fileList || []);
      if (files.length === 0) return;

      setNotice(null);
      setUpload({ progress: 0, count: files.length, name: files[0].name });

      try {
        const created = await uploadDataset(files, {
          onProgress: (progress) => setUpload((current) => (current ? { ...current, progress } : current)),
        });
        setUpload(null);
        setMessages([]);
        deepLink.current = true;
        await loadData();
        if (created?.warnings?.length) {
          setNotice({ kind: 'warn', text: created.warnings.join(' ') });
        }
      } catch (error) {
        setUpload(null);
        setNotice({ kind: 'error', text: error.message });
      }
    },
    [loadData],
  );

  const handleActivate = useCallback(
    async (id) => {
      setNotice(null);
      try {
        await activateDataset(id);
        setMessages([]);
        deepLink.current = true;
        await loadData();
      } catch (error) {
        setNotice({ kind: 'error', text: error.message });
      }
    },
    [loadData],
  );

  const handleRemove = useCallback(
    async (id) => {
      setNotice(null);
      try {
        await deleteDataset(id);
        setMessages([]);
        await loadData();
      } catch (error) {
        setNotice({ kind: 'error', text: error.message });
      }
    },
    [loadData],
  );

  /* -- render ------------------------------------------------------------- */

  const empty = datasets.length === 0;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className="brand-name">P1</span>
          <span className="brand-sub">Local analytics</span>
        </div>

        <div className="topbar-right">
          {active && <span className="topbar-dataset">{active.name}</span>}
          {!ready && !offline && (
            <span className="pill pill-loading">Warming up</span>
          )}
          {offline && <span className="pill pill-error">Backend offline</span>}
          <UploadButton onFiles={handleFiles} busy={Boolean(upload)} />
        </div>
      </header>

      <div className={`shell ${empty ? 'shell-empty' : ''}`}>
        {!empty && (
          <Rail
            datasets={datasets}
            schema={schema}
            suggestions={suggestions}
            busy={busy}
            onActivate={handleActivate}
            onRemove={handleRemove}
            onAsk={ask}
          />
        )}

        <main className="stage">
          <div className="stream" ref={streamRef}>
            {notice && (
              <div className={`notice notice-${notice.kind}`} role="status">
                <Icon name={notice.kind === 'error' ? 'alert' : 'file'} />
                <span>{notice.text}</span>
                <button
                  type="button"
                  className="icon-btn"
                  aria-label="Dismiss"
                  onClick={() => setNotice(null)}
                >
                  <Icon name="x" size={13} />
                </button>
              </div>
            )}

            <UploadProgress upload={upload} />

            {empty ? (
              <div className="empty">
                <div className="empty-lead">
                  <p className="empty-kicker">
                    <Icon name="spark" size={14} /> No data loaded
                  </p>
                  <h1>
                    Point P1 at a spreadsheet
                    <br />
                    and start asking questions.
                  </h1>
                  <p className="empty-body">
                    There is no sample data here. Everything you see is computed from the files you
                    add — the charts, the tables and the answers all come from your own columns.
                  </p>
                </div>

                <DropZone onFiles={handleFiles} busy={Boolean(upload)} />

                <div className="empty-foot">
                  <div>
                    <h2>What it works out on its own</h2>
                    <p>
                      Which columns are money, counts, categories, names and dates — then totals,
                      breakdowns, trends and distributions from that reading.
                    </p>
                  </div>
                  <div>
                    <h2>What you can ask</h2>
                    <p>
                      Anything shaped like an analysis: totals, comparisons, “by” questions, change
                      over time, outliers, correlations and raw previews.
                    </p>
                  </div>
                  <div>
                    <h2>What stays here</h2>
                    <p>
                      The file, the query and the answer. No cloud calls, no model downloads at
                      query time, no data leaving this machine.
                    </p>
                  </div>
                </div>
              </div>
            ) : (
              <>
                {insights && <InsightBoard insights={insights} />}

                <div className="thread-head">
                  <h2>Conversation</h2>
                  <p>
                    {messages.length === 0
                      ? 'Nothing asked yet. The charts above are the automatic reading of your file.'
                      : `${messages.filter((m) => m.role === 'user').length} question${
                          messages.filter((m) => m.role === 'user').length === 1 ? '' : 's'
                        } this session.`}
                  </p>
                </div>

                <div className="thread" aria-live="polite">
                  {messages.map((message) => (
                    <MessageCard
                      key={message.id}
                      message={message}
                      onAsk={ask}
                      busy={busy}
                    />
                  ))}
                  {busy && <Thinking />}
                </div>
              </>
            )}
          </div>

          {!empty && (
            <Composer
              value={draft}
              onChange={setDraft}
              onSend={ask}
              busy={busy}
              disabled={!ready}
              placeholder={
                ready
                  ? `Ask about ${active?.name.replace(/\.[^.]+$/, '') || 'your data'}…`
                  : 'Waiting for the local model…'
              }
            />
          )}
        </main>
      </div>
    </div>
  );
}

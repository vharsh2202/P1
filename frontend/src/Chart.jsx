/**
 * Inline SVG charts.
 *
 * There is no charting library here on purpose: the backend emits a compact
 * spec and this component draws it with plain SVG. That keeps the frontend
 * dependency-free, the bundle small, and every pixel under our control.
 *
 * Spec shape (from the backend):
 *   { type, title, subtitle, x_label, y_label, unit, points: [{label, value}] }
 *
 * Types: bar | hbar | line | area | histogram | scatter | grouped_bar
 */

import { useEffect, useMemo, useRef, useState } from 'react';

/* -------------------------------------------------------------------------- *
 * Formatting
 * -------------------------------------------------------------------------- */

function trimZero(text) {
  return text.replace(/\.0+$/, '').replace(/(\.\d*?)0+$/, '$1');
}

export function compactNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  const abs = Math.abs(number);
  if (abs >= 1e12) return `${trimZero((number / 1e12).toFixed(1))}T`;
  if (abs >= 1e9) return `${trimZero((number / 1e9).toFixed(1))}B`;
  if (abs >= 1e6) return `${trimZero((number / 1e6).toFixed(1))}M`;
  if (abs >= 1e3) return `${trimZero((number / 1e3).toFixed(1))}K`;
  if (abs >= 100) return Math.round(number).toLocaleString();
  if (Number.isInteger(number)) return String(number);
  return trimZero(number.toFixed(2));
}

export function formatValue(value, unit = 'number') {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  if (unit === 'money') {
    return `$${number.toLocaleString(undefined, {
      minimumFractionDigits: Math.abs(number) < 1000 ? 2 : 0,
      maximumFractionDigits: Math.abs(number) < 1000 ? 2 : 0,
    })}`;
  }
  if (unit === 'percent') return `${trimZero(number.toFixed(1))}%`;
  if (unit === 'count') return Math.round(number).toLocaleString();
  return number.toLocaleString(undefined, {
    maximumFractionDigits: Number.isInteger(number) ? 0 : 2,
  });
}

function axisFormatter(unit) {
  if (unit === 'money') return (v) => `$${compactNumber(v)}`;
  if (unit === 'percent') return (v) => `${compactNumber(v)}%`;
  return (v) => compactNumber(v);
}

/* -------------------------------------------------------------------------- *
 * Scale helpers
 * -------------------------------------------------------------------------- */

function niceTicks(min, max, target = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) {
    if (min === 0) return [0, 1];
    const pad = Math.abs(min) * 0.5;
    min -= pad;
    max += pad;
  }
  const span = max - min;
  const raw = span / Math.max(1, target);
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalised = raw / magnitude;
  const step =
    (normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 2.5 ? 2.5 : normalised <= 5 ? 5 : 10) *
    magnitude;

  const start = Math.floor(min / step) * step;
  const end = Math.ceil(max / step) * step;
  const ticks = [];
  for (let value = start; value <= end + step * 1e-6; value += step) {
    ticks.push(Math.abs(value) < step * 1e-9 ? 0 : Number(value.toFixed(10)));
  }
  return ticks;
}

function useMeasure() {
  const ref = useRef(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const element = ref.current;
    if (!element) return undefined;

    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect?.width;
      if (next) setWidth(Math.floor(next));
    });
    observer.observe(element);
    setWidth(Math.floor(element.getBoundingClientRect().width));
    return () => observer.disconnect();
  }, []);

  return [ref, width];
}

function truncate(text, limit) {
  const value = String(text ?? '');
  if (value.length <= limit) return value;
  return `${value.slice(0, Math.max(1, limit - 1))}…`;
}

/* -------------------------------------------------------------------------- *
 * Shared bits
 * -------------------------------------------------------------------------- */

const PALETTE = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)'];

function Grid({ ticks, scale, left, right, horizontal = true }) {
  return (
    <g>
      {ticks.map((tick) => {
        const position = scale(tick);
        return (
          <line
            key={`g${tick}`}
            x1={horizontal ? left : position}
            x2={horizontal ? right : position}
            y1={horizontal ? position : left}
            y2={horizontal ? position : right}
            className="chart-grid"
          />
        );
      })}
    </g>
  );
}

function Tooltip({ tip }) {
  if (!tip) return null;
  return (
    <div
      className="chart-tip"
      style={{ left: tip.x, top: tip.y }}
      role="status"
      aria-live="polite"
    >
      <span className="chart-tip-label">{tip.label}</span>
      <span className="chart-tip-value">{tip.value}</span>
    </div>
  );
}

/* -------------------------------------------------------------------------- *
 * Chart
 * -------------------------------------------------------------------------- */

export default function Chart({ spec, compact = false }) {
  const [measureRef, width] = useMeasure();
  const [tip, setTip] = useState(null);

  const height = compact ? 168 : 224;
  const points = spec?.points || [];
  const series = spec?.series || [];
  const type = spec?.type || 'bar';
  const unit = spec?.unit || 'number';
  const format = axisFormatter(unit);
  const full = (value) => formatValue(value, unit);

  const aria = useMemo(() => {
    if (type === 'grouped_bar' && series.length) {
      return `${spec.title}. ${series.length} series across ${series[0].points.length} categories.`;
    }
    if (!points.length) return spec?.title || 'Chart';
    const best = points.reduce((a, b) => (Number(b.value) > Number(a.value) ? b : a), points[0]);
    return `${spec.title}. ${points.length} points. Highest: ${best.label} at ${full(best.value)}.`;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spec, type, series, points, unit]);

  if (!spec) return null;

  return (
    <figure className={`chart ${compact ? 'chart-compact' : ''}`}>
      <figcaption className="chart-head">
        <h3 className="chart-title">{spec.title}</h3>
        {spec.subtitle && <p className="chart-sub">{spec.subtitle}</p>}
      </figcaption>

      <div className="chart-body" ref={measureRef}>
        {width > 0 && (
          <svg
            width={width}
            height={height}
            role="img"
            aria-label={aria}
            className="chart-svg"
            onMouseLeave={() => setTip(null)}
          >
            {type === 'hbar' ? (
              <HorizontalBars
                points={points}
                width={width}
                height={height}
                format={format}
                full={full}
                setTip={setTip}
              />
            ) : type === 'line' || type === 'area' ? (
              <LineChart
                points={points}
                width={width}
                height={height}
                format={format}
                full={full}
                fill={type === 'area'}
                setTip={setTip}
              />
            ) : type === 'scatter' ? (
              <Scatter
                points={points}
                width={width}
                height={height}
                format={format}
                full={full}
                unit={unit}
                setTip={setTip}
              />
            ) : type === 'grouped_bar' ? (
              <GroupedBars
                series={series}
                width={width}
                height={height}
                format={format}
                full={full}
                setTip={setTip}
              />
            ) : (
              <VerticalBars
                points={points}
                width={width}
                height={height}
                format={format}
                full={full}
                histogram={type === 'histogram'}
                setTip={setTip}
              />
            )}
          </svg>
        )}
        <Tooltip tip={tip} />
      </div>
    </figure>
  );
}

/* -------------------------------------------------------------------------- *
 * Vertical bars / histogram
 * -------------------------------------------------------------------------- */

function VerticalBars({ points, width, height, format, full, histogram, setTip }) {
  const pad = { top: 10, right: 10, bottom: 38, left: 52 };
  const plotWidth = Math.max(10, width - pad.left - pad.right);
  const plotHeight = Math.max(10, height - pad.top - pad.bottom);

  const values = points.map((p) => Number(p.value) || 0);
  const maxValue = Math.max(0, ...values);
  const ticks = niceTicks(0, maxValue || 1, 4);
  const top = ticks[ticks.length - 1] || 1;
  const y = (value) => pad.top + plotHeight - (value / top) * plotHeight;

  const slot = plotWidth / Math.max(1, points.length);
  const gap = histogram ? 1 : Math.min(18, slot * 0.28);
  const barWidth = Math.max(2, slot - gap);
  const rotate = slot < 62 && points.length > 4;

  return (
    <g>
      <Grid ticks={ticks} scale={y} left={pad.left} right={pad.left + plotWidth} />
      {ticks.map((tick) => (
        <text key={`t${tick}`} x={pad.left - 8} y={y(tick) + 4} className="chart-axis" textAnchor="end">
          {format(tick)}
        </text>
      ))}

      {points.map((point, index) => {
        const value = Number(point.value) || 0;
        const x = pad.left + index * slot + gap / 2;
        const barHeight = Math.max(value > 0 ? 2 : 0, pad.top + plotHeight - y(value));
        return (
          <g
            key={`${point.label}-${index}`}
            onMouseEnter={(event) =>
              setTip({
                x: event.nativeEvent.offsetX + 12,
                y: event.nativeEvent.offsetY - 8,
                label: point.label,
                value: full(value),
              })
            }
          >
            <rect
              x={x}
              y={pad.top}
              width={barWidth}
              height={plotHeight}
              fill="transparent"
            />
            <rect
              x={x}
              y={y(value)}
              width={barWidth}
              height={barHeight}
              rx={histogram ? 0 : 2}
              className="chart-bar"
            />
          </g>
        );
      })}

      <line
        x1={pad.left}
        x2={pad.left + plotWidth}
        y1={pad.top + plotHeight}
        y2={pad.top + plotHeight}
        className="chart-baseline"
      />

      {points.map((point, index) => {
        const x = pad.left + index * slot + slot / 2;
        const label = truncate(point.label, rotate ? 12 : Math.max(6, Math.floor(slot / 7)));
        return (
          <text
            key={`l${point.label}-${index}`}
            x={x}
            y={pad.top + plotHeight + 14}
            className="chart-label"
            textAnchor={rotate ? 'end' : 'middle'}
            transform={rotate ? `rotate(-38 ${x} ${pad.top + plotHeight + 14})` : undefined}
          >
            {label}
          </text>
        );
      })}
    </g>
  );
}

/* -------------------------------------------------------------------------- *
 * Horizontal bars
 * -------------------------------------------------------------------------- */

function HorizontalBars({ points, width, height, format, full, setTip }) {
  const labelWidth = Math.min(140, Math.max(72, width * 0.26));
  const pad = { top: 6, right: 58, bottom: 22, left: labelWidth + 10 };
  const plotWidth = Math.max(10, width - pad.left - pad.right);
  const plotHeight = Math.max(10, height - pad.top - pad.bottom);

  const values = points.map((p) => Number(p.value) || 0);
  const maxValue = Math.max(0, ...values) || 1;
  const ticks = niceTicks(0, maxValue, 3);
  const top = ticks[ticks.length - 1] || 1;
  const x = (value) => pad.left + (value / top) * plotWidth;

  const slot = plotHeight / Math.max(1, points.length);
  const barHeight = Math.max(3, Math.min(26, slot - 6));

  return (
    <g>
      <Grid ticks={ticks} scale={x} left={pad.top} right={pad.top + plotHeight} horizontal={false} />
      {points.map((point, index) => {
        const value = Number(point.value) || 0;
        const y = pad.top + index * slot + slot / 2;
        return (
          <g
            key={`${point.label}-${index}`}
            onMouseEnter={(event) =>
              setTip({
                x: event.nativeEvent.offsetX + 12,
                y: event.nativeEvent.offsetY - 8,
                label: point.label,
                value: full(value),
              })
            }
          >
            <rect x={0} y={y - slot / 2} width={width} height={slot} fill="transparent" />
            <rect
              x={pad.left}
              y={y - barHeight / 2}
              width={Math.max(1, x(value) - pad.left)}
              height={barHeight}
              rx={2}
              className="chart-bar"
            />
            <text
              x={pad.left - 8}
              y={y + 4}
              className="chart-label"
              textAnchor="end"
            >
              {truncate(point.label, Math.floor(labelWidth / 6.6))}
            </text>
            <text x={x(value) + 7} y={y + 4} className="chart-value">
              {full(value)}
            </text>
          </g>
        );
      })}
      <line x1={pad.left} x2={pad.left} y1={pad.top} y2={pad.top + plotHeight} className="chart-baseline" />
    </g>
  );
}

/* -------------------------------------------------------------------------- *
 * Line / area
 * -------------------------------------------------------------------------- */

function LineChart({ points, width, height, format, full, fill, setTip }) {
  const pad = { top: 12, right: 14, bottom: 30, left: 52 };
  const plotWidth = Math.max(10, width - pad.left - pad.right);
  const plotHeight = Math.max(10, height - pad.top - pad.bottom);

  const values = points.map((p) => Number(p.value) || 0);
  const minValue = Math.min(0, ...values);
  const maxValue = Math.max(0, ...values);
  const ticks = niceTicks(minValue, maxValue || 1, 4);
  const low = ticks[0];
  const high = ticks[ticks.length - 1] || 1;
  const span = high - low || 1;

  const x = (index) =>
    pad.left + (points.length <= 1 ? plotWidth / 2 : (index / (points.length - 1)) * plotWidth);
  const y = (value) => pad.top + plotHeight - ((value - low) / span) * plotHeight;

  const line = points.map((point, index) => `${x(index)},${y(Number(point.value) || 0)}`).join(' ');
  const area = [
    `${x(0)},${y(low)}`,
    ...points.map((point, index) => `${x(index)},${y(Number(point.value) || 0)}`),
    `${x(points.length - 1)},${y(low)}`,
  ].join(' ');

  const labelEvery = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(plotWidth / 74))));

  return (
    <g>
      <Grid ticks={ticks} scale={y} left={pad.left} right={pad.left + plotWidth} />
      {ticks.map((tick) => (
        <text key={`t${tick}`} x={pad.left - 8} y={y(tick) + 4} className="chart-axis" textAnchor="end">
          {format(tick)}
        </text>
      ))}

      {fill && <polygon points={area} className="chart-area" />}
      <polyline points={line} className="chart-line" fill="none" />

      {points.map((point, index) => {
        const value = Number(point.value) || 0;
        const showDot = points.length <= 14 || index === 0 || index === points.length - 1;
        return (
          <g key={`${point.label}-${index}`}>
            <circle
              cx={x(index)}
              cy={y(value)}
              r={12}
              fill="transparent"
              onMouseEnter={(event) =>
                setTip({
                  x: event.nativeEvent.offsetX + 12,
                  y: event.nativeEvent.offsetY - 8,
                  label: point.label,
                  value: full(value),
                })
              }
            />
            {showDot && <circle cx={x(index)} cy={y(value)} r={2.6} className="chart-dot" />}
          </g>
        );
      })}

      {points.map((point, index) =>
        index % labelEvery === 0 ? (
          <text
            key={`l${point.label}-${index}`}
            x={x(index)}
            y={pad.top + plotHeight + 16}
            className="chart-label"
            textAnchor="middle"
          >
            {truncate(point.label, 10)}
          </text>
        ) : null,
      )}
    </g>
  );
}

/* -------------------------------------------------------------------------- *
 * Scatter
 * -------------------------------------------------------------------------- */

function Scatter({ points, width, height, format, full, unit, setTip }) {
  const pad = { top: 12, right: 16, bottom: 32, left: 56 };
  const plotWidth = Math.max(10, width - pad.left - pad.right);
  const plotHeight = Math.max(10, height - pad.top - pad.bottom);

  const xs = points.map((p) => (p.x !== undefined ? Number(p.x) : Number(p.label)) || 0);
  const ys = points.map((p) => Number(p.value) || 0);
  const xTicks = niceTicks(Math.min(...xs), Math.max(...xs), 4);
  const yTicks = niceTicks(Math.min(0, ...ys), Math.max(...ys), 4);

  const xLow = xTicks[0];
  const xHigh = xTicks[xTicks.length - 1] || 1;
  const yLow = yTicks[0];
  const yHigh = yTicks[yTicks.length - 1] || 1;

  const x = (value) => pad.left + ((value - xLow) / (xHigh - xLow || 1)) * plotWidth;
  const y = (value) => pad.top + plotHeight - ((value - yLow) / (yHigh - yLow || 1)) * plotHeight;

  return (
    <g>
      <Grid ticks={yTicks} scale={y} left={pad.left} right={pad.left + plotWidth} />
      {yTicks.map((tick) => (
        <text key={`y${tick}`} x={pad.left - 8} y={y(tick) + 4} className="chart-axis" textAnchor="end">
          {format(tick)}
        </text>
      ))}
      {xTicks.map((tick) => (
        <text
          key={`x${tick}`}
          x={x(tick)}
          y={pad.top + plotHeight + 16}
          className="chart-axis"
          textAnchor="middle"
        >
          {format(tick)}
        </text>
      ))}

      {points.map((point, index) => (
        <circle
          key={`p${index}`}
          cx={x(xs[index])}
          cy={y(ys[index])}
          r={12}
          fill="transparent"
          onMouseEnter={(event) =>
            setTip({
              x: event.nativeEvent.offsetX + 12,
              y: event.nativeEvent.offsetY - 8,
              label: point.label,
              value: full(ys[index]),
            })
          }
        >
          <title>{`${point.label}: ${full(ys[index])}`}</title>
        </circle>
      ))}
      {points.map((point, index) => (
        <circle
          key={`d${index}`}
          cx={x(xs[index])}
          cy={y(ys[index])}
          r={3}
          className="chart-scatter-dot"
          pointerEvents="none"
        />
      ))}
    </g>
  );
}

/* -------------------------------------------------------------------------- *
 * Grouped bars
 * -------------------------------------------------------------------------- */

function GroupedBars({ series, width, height, format, full, setTip }) {
  const pad = { top: 12, right: 10, bottom: 44, left: 52 };
  const plotWidth = Math.max(10, width - pad.left - pad.right);
  const plotHeight = Math.max(10, height - pad.top - pad.bottom);

  const labels = series[0]?.points?.map((p) => p.label) || [];
  const allValues = series.flatMap((s) => s.points.map((p) => Number(p.value) || 0));
  const ticks = niceTicks(0, Math.max(...allValues, 1), 4);
  const top = ticks[ticks.length - 1] || 1;
  const y = (value) => pad.top + plotHeight - (value / top) * plotHeight;

  const groupSlot = plotWidth / Math.max(1, labels.length);
  const seriesCount = Math.max(1, series.length);
  const barWidth = Math.max(2, Math.min(20, (groupSlot * 0.72) / seriesCount));
  const rotate = groupSlot < 70 && labels.length > 4;

  return (
    <g>
      <Grid ticks={ticks} scale={y} left={pad.left} right={pad.left + plotWidth} />
      {ticks.map((tick) => (
        <text key={`t${tick}`} x={pad.left - 8} y={y(tick) + 4} className="chart-axis" textAnchor="end">
          {format(tick)}
        </text>
      ))}

      {labels.map((label, groupIndex) => {
        const centre = pad.left + groupIndex * groupSlot + groupSlot / 2;
        const groupWidth = barWidth * seriesCount;
        return (
          <g key={label}>
            {series.map((entry, seriesIndex) => {
              const point = entry.points[groupIndex];
              const value = Number(point?.value) || 0;
              const x = centre - groupWidth / 2 + seriesIndex * barWidth;
              return (
                <rect
                  key={entry.label}
                  x={x}
                  y={y(value)}
                  width={Math.max(1, barWidth - 2)}
                  height={Math.max(0, pad.top + plotHeight - y(value))}
                  rx={1.5}
                  style={{ fill: PALETTE[seriesIndex % PALETTE.length] }}
                  onMouseEnter={(event) =>
                    setTip({
                      x: event.nativeEvent.offsetX + 12,
                      y: event.nativeEvent.offsetY - 8,
                      label: `${label} · ${entry.label}`,
                      value: full(value),
                    })
                  }
                />
              );
            })}
          </g>
        );
      })}

      <line
        x1={pad.left}
        x2={pad.left + plotWidth}
        y1={pad.top + plotHeight}
        y2={pad.top + plotHeight}
        className="chart-baseline"
      />

      {labels.map((label, groupIndex) => {
        const centre = pad.left + groupIndex * groupSlot + groupSlot / 2;
        return (
          <text
            key={`l${label}`}
            x={centre}
            y={pad.top + plotHeight + 14}
            className="chart-label"
            textAnchor={rotate ? 'end' : 'middle'}
            transform={rotate ? `rotate(-38 ${centre} ${pad.top + plotHeight + 14})` : undefined}
          >
            {truncate(label, rotate ? 12 : 9)}
          </text>
        );
      })}
    </g>
  );
}

/* -------------------------------------------------------------------------- *
 * Legend (rendered outside the SVG by callers that want one)
 * -------------------------------------------------------------------------- */

export function ChartLegend({ series }) {
  if (!series?.length) return null;
  return (
    <ul className="chart-legend">
      {series.slice(0, 4).map((entry, index) => (
        <li key={entry.label}>
          <span className="legend-swatch" style={{ background: PALETTE[index % PALETTE.length] }} />
          {entry.label}
        </li>
      ))}
    </ul>
  );
}

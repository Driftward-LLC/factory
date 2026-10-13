import { useEffect, useRef, useState } from 'react';
import { Notice, Heading } from '@driftward/ui';
import { count } from './model.js';

const metrics = [
  ['prs_recorded', 'PRs recorded', ''],
  ['verified_attempts', 'Verified attempts', '6 3'],
  ['repair_attempts', 'Repair attempts', '2 4'],
  ['failed_attempts', 'Failed attempts', '10 3 2 3'],
];
const dayLabel = (date) => new Date(date + 'T00:00:00Z').toLocaleDateString(undefined, {
  month: 'short', day: 'numeric', timeZone: 'UTC',
});
const known = (value) => Number.isInteger(value) && value >= 0;

export default function AnalyticsTrend({ series }) {
  const chart = useRef(null);
  const [width, setWidth] = useState(800);
  const [selectedDate, setSelectedDate] = useState(null);
  const points = Array.isArray(series?.points) ? series.points : [];
  useEffect(() => {
    if (!chart.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(1, entry.contentRect.width)));
    observer.observe(chart.current);
    return () => observer.disconnect();
  }, [points.length > 0]);

  const selected = points.findIndex((point) => point.date === selectedDate);
  const index = selected >= 0 ? selected : points.length - 1;
  const day = points[index];
  const values = points.flatMap((point) => metrics.map(([key]) => point[key])).filter(known);
  const step = Math.max(1, Math.ceil(Math.max(1, ...values) / 4));
  const ceiling = step * 4;
  const left = Math.max(44, String(ceiling).length * 14 + 10);
  const right = Math.max(left + 1, width - 16);
  const top = 16, bottom = 204, height = 240;
  const x = (i) => points.length > 1 ? left + (right - left) * i / (points.length - 1) : (left + right) / 2;
  const y = (value) => bottom - (bottom - top) * value / ceiling;
  const tickIndices = [...new Set([0, ...(width > 480 ? [Math.floor((points.length - 1) / 2)] : []), points.length - 1])];
  const incomplete = metrics.filter(([key]) => series?.coverage?.[key]?.complete !== true || points.some((point) => !known(point[key])));
  const noActivity = points.length > 0 && values.length > 0 && values.every((value) => value === 0);
  function pickDay(event) {
    const bounds = chart.current.getBoundingClientRect();
    const localX = (event.clientX - bounds.left) * width / bounds.width;
    const next = Math.max(0, Math.min(points.length - 1, Math.round((localX - left) / (right - left) * (points.length - 1))));
    setSelectedDate(points[next].date);
  }
  function keyDay(event) {
    const next = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: points.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    setSelectedDate(points[Math.max(0, Math.min(points.length - 1, next))].date);
  }
  return (
    <section id="analytics-trend" className="panel analytics-trend" aria-labelledby="analytics-trend-title">
      <div className="section-heading">
        <div><Heading level={2} id="analytics-trend-title">Daily activity</Heading>
          <p className="muted">Recorded events by UTC day. Includes work started before this window.</p></div>
      </div>
      {!points.length ? <Notice id="analytics-trend-unavailable">Daily activity is unavailable for this snapshot.</Notice> : <>
        <ul className="analytics-trend-legend" aria-label="Chart series">
          {metrics.map(([key, title, dash]) => <li key={key}>
            <svg viewBox="0 0 24 12" aria-hidden="true"><line x1="0" x2="24" y1="6" y2="6" className={'trend-line ' + key} strokeDasharray={dash} /></svg>
            <span>{title}</span>
          </li>)}
        </ul>
        <Notice id="analytics-trend-unavailable">{incomplete.length
          ? `Incomplete dated evidence: ${incomplete.map(([, title]) => title.toLowerCase()).join(', ')}. Unavailable values appear as gaps, not zero activity.` : ''}</Notice>
        <svg id="analytics-trend-chart" ref={chart} viewBox={`0 0 ${width} ${height}`} role="img"
          aria-labelledby="analytics-trend-chart-title analytics-trend-chart-description" tabIndex={0}
          onPointerMove={pickDay} onClick={pickDay} onKeyDown={keyDay}>
          <title id="analytics-trend-chart-title">Daily recorded activity</title>
          <desc id="analytics-trend-chart-description">PRs recorded, verified attempts, repair attempts and failed attempts per UTC day. Use left and right arrow keys to inspect days, Home and End for the first and last day. Exact values are also in Daily data below.</desc>
          <g aria-hidden="true">
            {[0, 1, 2, 3, 4].map((tick) => <g key={tick}>
              <line className="trend-grid" x1={left} x2={right} y1={y(tick * step)} y2={y(tick * step)} />
              <text className="trend-axis" x={left - 10} y={y(tick * step) + 4} textAnchor="end">{tick * step}</text>
            </g>)}
            {metrics.map(([key, , dash]) => {
              let connected = false;
              const path = points.map((point, i) => {
                if (!known(point[key])) { connected = false; return ''; }
                const command = connected ? 'L' : 'M';
                connected = true;
                return `${command}${x(i)},${y(point[key])}`;
              }).join(' ');
              return path.trim() && <g key={key}>
                <path data-series={key} className={'trend-line ' + key} d={path} strokeDasharray={dash} />
                {points.map((point, i) => known(point[key]) &&
                  (point[key] > 0 || i === index || (!known(points[i - 1]?.[key]) && !known(points[i + 1]?.[key])))
                  ? <circle key={point.date} className={'trend-dot ' + key} cx={x(i)} cy={y(point[key])} r={i === index ? 4 : 2.5} /> : null)}
              </g>;
            })}
            <line className="trend-cursor" x1={x(index)} x2={x(index)} y1={top} y2={bottom} />
            {tickIndices.map((i) => <text key={i} className="trend-axis" x={x(i)} y={height - 10}
              textAnchor={i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}>
              {dayLabel(points[i].date)}{points[i].partial ? '*' : ''}
            </text>)}
          </g>
        </svg>
        {noActivity && <p id="analytics-trend-empty" className="muted">No activity recorded in the available series during this window.</p>}
        <div id="analytics-trend-detail" className="analytics-trend-detail" data-date={day.date} role="status" aria-live="polite">
          <label htmlFor="analytics-trend-day">Inspect day (UTC)
            <select id="analytics-trend-day" value={day.date} onChange={(event) => setSelectedDate(event.target.value)}>
              {points.map((point) => <option key={point.date} value={point.date}>{point.date}{point.partial ? ' · partial day' : ''}</option>)}
            </select>
          </label>
          <dl>{metrics.map(([key, title]) => <div key={key}><dt>{title}</dt><dd>{count(day[key])}</dd></div>)}</dl>
        </div>
        <p className="muted analytics-trend-note">* Partial UTC day. PRs use their first recorded association; other series count attempts. These activity counts can differ from the attempt-creation cohort above. CI pass rates and reviewer acceptance are not inferred from these lines.</p>
        <details className="analytics-daily-data"><summary>Daily data</summary>
          <div className="table-wrap" role="region" aria-label="Daily recorded activity values" tabIndex={0}>
            <table id="analytics-trend-table"><caption className="sr-only">Recorded activity per UTC day</caption>
              <thead><tr><th scope="col">Date (UTC)</th>{metrics.map(([key, title]) => <th key={key} scope="col">{title}</th>)}</tr></thead>
              <tbody>{points.map((point) => <tr key={point.date}>
                <th scope="row">{point.date}{point.partial ? ' · partial' : ''}</th>
                {metrics.map(([key]) => <td key={key}>{count(point[key])}</td>)}
              </tr>)}</tbody>
            </table>
          </div>
        </details>
      </>}
    </section>
  );
}

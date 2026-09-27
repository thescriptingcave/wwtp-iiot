/**
 * A trend, drawn by hand in SVG.
 *
 * No charting library, for the same reason the Grafana dashboards use the
 * built-in PostgreSQL datasource rather than a plugin: **a dependency is a
 * thing that can break, and a dashboard that fails to render during an incident
 * is worse than an ugly one.** This file is 90 lines and has no transitive
 * dependencies, and it draws exactly the one thing a trend needs to draw.
 *
 * The one non-obvious part is the gap handling. `points` carries `null` for a
 * minute with no reading, and a `null` **breaks the path** rather than being
 * skipped. Skipping would connect the last reading before the gap to the first
 * one after it, which draws a straight line across an interval the page knows
 * nothing about — and on thirteen signals that line would be a lie about a week
 * of data. The band is drawn behind the line, and the gap shows as the band
 * with nothing on it, which reads correctly: "no information here".
 */
import type { Point } from '../lib/queries';

interface Props {
  points: Point[];
  /** Contract band, drawn as a shaded region. Omitted when there is none. */
  band?: { low: number; high: number } | null;
  height?: number;
  label: string;
}

const W = 320;
const PAD = 4;

export function Sparkline({ points, band, height = 56, label }: Props) {
  const present = points.filter((p) => p.mean !== null);
  if (present.length === 0) {
    return (
      <div className="spark spark--empty" role="img" aria-label={`${label}: no data`}>
        no data in this window
      </div>
    );
  }

  const values = present.map((p) => p.mean as number);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (band) {
    // The band must be in frame even when the data sits inside it, or the panel
    // shows a line crossing a boundary the reader cannot see.
    lo = Math.min(lo, band.low);
    hi = Math.max(hi, band.high);
  }
  // A perfectly flat signal would divide by zero. Give it a nominal span so it
  // draws as a centred line rather than as a divide-by-zero NaN path, which
  // renders as nothing at all — a panel that is empty because the value is
  // constant is the most misleading empty panel there is.
  if (hi === lo) {
    lo -= 1;
    hi += 1;
  }

  const n = Math.max(1, points.length - 1);
  const x = (i: number) => PAD + (i / n) * (W - 2 * PAD);
  const y = (v: number) =>
    PAD + (1 - (v - lo) / (hi - lo)) * (height - 2 * PAD);

  // Split into runs of consecutive present points. Each run is one `M…L…`.
  const runs: string[] = [];
  let current: string[] = [];
  points.forEach((p, i) => {
    if (p.mean === null) {
      if (current.length > 1) runs.push(current.join(' '));
      current = [];
      return;
    }
    current.push(`${current.length === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(p.mean).toFixed(1)}`);
  });
  if (current.length > 1) runs.push(current.join(' '));

  const bands = band
    ? [
        `M${PAD},${y(band.high).toFixed(1)} L${W - PAD},${y(band.high).toFixed(1)} ` +
          `L${W - PAD},${y(band.low).toFixed(1)} L${PAD},${y(band.low).toFixed(1)} Z`,
      ]
    : [];

  return (
    <svg
      className="spark"
      viewBox={`0 0 ${W} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`${label}: ${present.length} of ${points.length} minutes reporting, ` +
        `between ${lo.toFixed(2)} and ${hi.toFixed(2)}`}
    >
      {bands.map((d, i) => (
        <path key={`b${i}`} d={d} className="spark__band" />
      ))}
      {runs.map((d, i) => (
        <path key={`r${i}`} d={d} className="spark__line" />
      ))}
    </svg>
  );
}

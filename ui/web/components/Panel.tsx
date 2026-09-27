/**
 * One signal: its current value, how long ago that was, and its trend.
 *
 * The staleness line is not decoration. See `lib/format.ts::staleAfter` and
 * thread 8 of the learning log — a deadband means a healthy steady signal and a
 * failed instrument produce the same observation, so a panel that shows a value
 * without showing its age is claiming a currency it cannot verify.
 */
import { Sparkline } from './Sparkline';
import { ago, num, staleAfter } from '../lib/format';
import type { Latest, Point } from '../lib/queries';
import type { Signal } from '../lib/types';

interface Props {
  signal: Signal;
  latest: Latest | undefined;
  points: Point[];
}

export function Panel({ signal, latest, points }: Props) {
  const age = latest?.age_s ?? null;
  const stale = staleAfter(age);
  const value = latest?.value ?? null;
  const inBand =
    signal.band && value !== null
      ? value >= signal.band.low && value <= signal.band.high
      : null;

  return (
    <article className="panel">
      <header className="panel__head">
        <h3 className="panel__label" title={signal.id}>
          {signal.label}
        </h3>
        <span className="panel__id">{signal.id}</span>
      </header>

      <p className="panel__value">
        <span className="panel__number">{num(value, signal.precision)}</span>
        <span className="panel__eu">{signal.eu}</span>
        {inBand === false && (
          <span className="tag tag--warn" title="outside the contract's normal band">
            outside band
          </span>
        )}
      </p>

      <p className={stale ? 'panel__age panel__age--stale' : 'panel__age'}>
        {ago(age)}
        {stale && (
          <span className="tag tag--stale" title="no reading for over 15 minutes">
            silent
          </span>
        )}
      </p>

      <Sparkline points={points} band={signal.band} label={signal.label} />
    </article>
  );
}

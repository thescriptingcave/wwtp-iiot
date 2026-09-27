/**
 * Number formatting.
 *
 * Small module, and it exists for one reason: `null` must render as something
 * visibly different from `0`. This project's headline bug class is a plausible
 * wrong number rather than an error, and `null?.toFixed(2) ?? ''` rendering an
 * empty cell next to a real `0.00` is exactly the sort of thing an operator
 * learns to skip past. So absence is rendered as a dash, always, and the
 * styling says so.
 */

/** Render a value at the contract's precision, or a dash when there is none. */
export function num(
  value: number | null | undefined,
  precision = 2,
  unit?: string,
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  const text = value.toFixed(precision);
  return unit ? `${text} ${unit}` : text;
}

/** A duration in seconds, as something a person reads. */
export function ago(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
    return 'never';
  }
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/**
 * Whether a signal's silence is worth flagging.
 *
 * A function rather than a constant because the threshold is `sample_ms`-derived
 * in principle and a constant in practice: 15 minutes is three orders of
 * magnitude above every `sample_ms` in the contract, so anything past it is a
 * gateway that stopped writing rather than a process that settled.
 *
 * This exists because of thread 8. Thirteen of the 57 signals produce exactly one
 * reading across a seeded week — a healthy steady signal and a failed
 * instrument are the same observation — so "how long since this last reported" is
 * the only honest way to show absence, and a panel that draws a line that stops
 * is showing a fault that is not there.
 */
export function staleAfter(seconds: number | null | undefined): boolean {
  if (seconds === null || seconds === undefined) return true;
  return seconds > 15 * 60;
}

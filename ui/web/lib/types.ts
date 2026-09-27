/**
 * The shape of `lib/contract.json`, written out by hand.
 *
 * The file itself is generated and its *contents* are checked against
 * `contracts/tags.yaml` by `python -m ui.web.generate_page --check`, which runs
 * in CI. This is the one hand-written description of it, and it exists so that
 * a change to the generator's output shape is a **type error** rather than a
 * runtime `undefined` on a panel.
 *
 * It is not derived from the JSON. Generating a `.d.ts` from a JSON file needs
 * either a build step or a hand-written generator, and both are more machinery
 * than the twenty lines below — which is the same argument as "no charting
 * library" and "no Grafana plugin": the dependency is only worth it when the
 * thing it replaces is bigger than the thing it costs.
 */

export interface Band {
  low: number;
  high: number;
}

export interface Range {
  min: number;
  max: number;
}

export interface Signal {
  /** `AREA:EQUIPMENT:FIELD` — the machine address, and the query's only key. */
  id: string;
  /** The contract's short name, e.g. `do_mg_l`. */
  label: string;
  eu: string;
  precision: number;
  writable: boolean;
  equipment: string | null;
  sample_ms: number;
  band: Band | null;
  range: Range;
}

export interface Area {
  id: string;
  name: string;
  signals: Signal[];
}

export interface Contract {
  areas: Area[];
  counts: { areas: number; signals: number };
}

import contractJson from './contract.json';

export const contract = contractJson as Contract;

/** Every signal id, flattened. The order is the file's, which is sorted. */
export const allSignals: Signal[] = contract.areas.flatMap((a) => a.signals);

export const allSignalIds: string[] = allSignals.map((s) => s.id);

export function signalById(id: string): Signal | undefined {
  return allSignals.find((s) => s.id === id);
}

/**
 * The signals whose ids appear in `ids`, in the contract's order.
 *
 * A lookup rather than a map because this is called once per panel during a
 * render and the list is 57 long; a `Map` would be the right call at 5 700 and
 * this is not that.
 */
export function signalsIn(ids: Iterable<string>): Signal[] {
  const wanted = new Set(ids);
  return allSignals.filter((s) => wanted.has(s.id));
}

"""Building windows by hand, for tests, for the course, and for the coverage report.

A `Window` is three fields and a list of `Sample`s, which sounds too simple to
need a module — and then a test writes `[2.0, 1.9, 1.8, 1.7, 1.6]` and the reader
has to do the arithmetic to check that the fixture matches the assertion. Three
places in this project needed the same helpers, and a cross-test import is not a
thing that works, so they live here.

The functions describe *intent* rather than data:

    ramp(-0.15, hours=2)      "DO falls 0.15 mg/L per hour for two hours"
    step([1.0, 2.0, 3.0])     "three readings a minute apart"
    paired(a, b)              "two protocol faces, same instants"

which is the difference between a test that can be checked and one that has to be
trusted.
"""

from __future__ import annotations

from collections.abc import Sequence

from alarms.base import AlarmRule, Sample, Window

#: A fixed epoch so that fixtures do not depend on when the suite ran. 2023-11-14,
#: chosen because it is not a round number and nothing in the project keys off it.
T0 = 1_700_000_000.0


def step(
    values: Sequence[float | None],
    *,
    dt: float = 60.0,
    start: float = T0,
    quality: int = 0,
) -> tuple[Sample, ...]:
    """One sample per value, ``dt`` seconds apart.

    ``None`` produces a valueless reading, which is how a Bad instrument is
    represented (`sql/00-foundations/00-03_quality_is_data.md`).
    """
    return tuple(
        Sample(ts=start + i * dt, value=v, quality=quality)
        for i, v in enumerate(values)
    )


def ramp(
    per_hour: float,
    hours: float,
    *,
    start: float = 100.0,
    dt: float = 60.0,
    origin: float = T0,
) -> tuple[Sample, ...]:
    """A straight line at ``per_hour``, sampled every ``dt`` seconds.

    ``dt`` is the parameter that matters. The real plant produces readings about
    six minutes apart for dissolved oxygen and thousands a second for air flow,
    and a rate computed by differencing points means something different on each —
    which is why `slope_per_hour` fits against real timestamps rather than
    against sample index.
    """
    n = int(hours * 3600 / dt) + 1
    return step(
        [start + per_hour * (i * dt) / 3600.0 for i in range(n)],
        dt=dt, start=origin,
    )


def paired(
    a: Sequence[float],
    b: Sequence[float],
    *,
    dt: float = 60.0,
    start: float = T0,
) -> tuple[Sample, ...]:
    """Two observations of one signal at the *same* timestamps.

    That pairing is the entire mechanism behind `cross_validation`, and it is
    only expressible because `source` is part of `reading`'s primary key. Two
    protocol faces, two values, one instant.
    """
    if len(a) != len(b):
        raise ValueError(f"paired() needs equal lengths, got {len(a)} and {len(b)}")
    out: list[Sample] = []
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        out.append(Sample(ts=start + i * dt, value=float(x)))
        out.append(Sample(ts=start + i * dt, value=float(y)))
    return tuple(out)


def window(
    samples: Sequence[Sample],
    signal_id: str = "A:1:X",
    *,
    now: float | None = None,
) -> Window:
    """A window over ``samples``, clocked at the last sample unless told otherwise.

    ``now`` is how you ask "what did this look like two hours ago", which is the
    question a silence detector asks and the one that makes an empty window
    possible without hand-building one.
    """
    points = tuple(samples)
    return Window(
        signal_id=signal_id,
        points=points,
        now=points[-1].ts if now is None else now,
    )


def silent(
    signal_id: str = "A:1:X",
    *,
    silence_s: float,
    now: float | None = None,
) -> Window:
    """A window whose newest reading is ``silence_s`` seconds old.

    ``now`` defaults to ``T0 + silence_s``, which is the only thing that makes
    the window silent. Pass it explicitly when you need an absolute clock — for
    instance to ask the same question about two signals whose last readings
    arrived at different times.
    """
    return Window(
        signal_id=signal_id,
        points=step([1.0, 1.0]),
        now=T0 + silence_s if now is None else now,
    )


def rule(detector: str, **params: object) -> AlarmRule:
    """A throwaway rule for one detector.

    Sensible defaults for `params` for every detector, so that a test can say
    `rule("trend", per_hour=0.15)` without also having to supply `min_points`
    and `direction` to reach the branch it is actually about.
    """
    # `direction` is deliberately absent. Every detector that uses it has its own
    # default (`trend` down, `single_point_threshold` high, and
    # `deviation_from_baseline` *both* — which was the whole point: a shared
    # default here is what let `influent_lift_current_anomaly` believe it had
    # declared a direction when the rule set it and the detector dropped it.
    defaults: dict[str, object] = {
        "limit": 1.0, "hysteresis": 0.0,
        "tolerance": 1.0, "per_hour": 1.0, "min_points": 3,
        "horizon_s": 60.0, "min_count": 1, "max_silence_s": 60.0,
        "stuck_s": 0.0, "deadband": 0.0, "expected": "running",
        "stopped_value": 0, "baseline": None, "unit": "",
    }
    for key, value in defaults.items():
        params.setdefault(key, value)
    return AlarmRule(
        id=f"t_{detector}",
        signal_id="A:1:X",
        detector=detector,
        severity="warning",
        message="test",
        params=params,
    )


__all__ = ["T0", "AlarmRule", "paired", "ramp", "rule", "silent", "step",
           "window"]

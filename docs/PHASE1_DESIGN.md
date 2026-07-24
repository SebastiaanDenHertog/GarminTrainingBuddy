# V5 Phase 1 technical design

## Purpose

Phase 1 builds trustworthy measurements before the coach is allowed to use
personal pace, power and swim targets. The AI remains a proposal generator; the
measurement system is deterministic Python and SQLite.

## SQLite tables

### `planned_sessions`

A durable copy of planner-managed sessions, including Garmin workout and
schedule IDs, duration, role, phase, status, fingerprint and optional benchmark
ID.

### `activities`

Normalized Garmin activity summaries. The table stores performance fields and
parsed coach feedback. Full private activity-description text is not stored by
default.

### `session_matches`

One-to-one links between planned sessions and completed activities. Every match
contains a 0–100 score, matching method, date difference, duration ratio, name
similarity and human-readable reasons.

### `recovery_daily`

Daily readiness, sleep, HRV, resting-heart-rate delta and RED/AMBER/GREEN status
from advisor reports.

### `benchmark_results`

Normalized benchmark results with units, confidence and extraction method. The
raw supporting details are JSON, not free-form Garmin notes.

### `analysis_snapshots`

Immutable analysis outputs used later to evaluate whether a coaching change
actually improved performance.

## Matching model

Sport must match. Candidates outside the configured day tolerance are rejected.
The score then combines:

- exact date;
- exact Garmin workout ID, when available;
- duration similarity;
- normalized name similarity;
- role text in the completed activity name.

All candidate pairs are sorted globally and assigned greedily so a completed
activity cannot satisfy two planned sessions on a double-training day.

The default acceptance threshold is 55. Dashboard quality bands are:

- high: 85–100;
- medium: 70–84.99;
- low: 55–69.99.

## Performance trends

For run, bike and swim, the current 14 days are compared with the preceding 14
days. Phase 1 stores:

- sport minutes and volume change;
- efficiency change;
- Garmin VO2-max mean and absolute change when available;
- sample counts;
- a status and confidence value.

The efficiency measure is:

- bike with power: average power / average heart rate;
- run: average speed / average heart rate;
- swim: speed, with heart-rate normalization when Garmin supplies usable HR.

These are trend indicators, not laboratory measurements. Route, wind, terrain,
temperature, equipment and pool conditions can influence them.

## Recovery trend

The most recent seven days are compared with the preceding seven days for:

- Training Readiness;
- sleep score;
- HRV ratio;
- RED and AMBER day counts.

This trend is displayed but does not replace the existing daily safety rules.

## Benchmarks

Available protocols:

- `run_aerobic_30`
- `run_threshold_20`
- `bike_aerobic_45`
- `bike_threshold_20`
- `swim_css_400_200`

Automatic benchmark scheduling is environment-controlled. Due protocols replace
an existing session of the same sport. Threshold/CSS tests should only be
enabled after reviewing the weekly hard-session distribution.

Benchmark extraction prefers a Garmin lap matching the prescribed duration or
distance. A summary fallback is marked with lower confidence. Phase 2 should use
only high-confidence repeatable results for automatic target updates.

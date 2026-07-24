# Workout targets and weekly Garmin publishing

## Target sources

V5 Phase 1.1 does not infer thresholds from arbitrary activities. It uses an
explicit manual value or a compatible benchmark in `training.db` whose
confidence meets `WORKOUT_TARGET_MIN_CONFIDENCE` (default `0.65`).

| Target | Preferred source | Garmin usage |
|---|---|---|
| Run threshold pace | `run_threshold_20` | Pace range on hard run intervals |
| Run threshold HR | `run_threshold_20` | HR on easy/long/brick runs; fallback for hard intervals |
| Cycling FTP | `bike_threshold_20` with power | Power range on hard bike intervals |
| Cycling threshold HR | `bike_threshold_20` | HR on easy/long rides; fallback for hard intervals |
| Swim CSS | `swim_css_400_200` | Pace guidance per 100 m in the description |

A 20-minute maximal run is normally faster than sustainable threshold pace, so
its measured pace is multiplied by `1.03` by default. A 20-minute cycling power
result is multiplied by `0.95` to form the initial FTP estimate. Both factors are
configurable under `workout_targets.benchmark_conversions`.

Garmin workout steps accept one primary target. Secondary guidance is written in
the workout description and displayed in the dashboard. The default priority is:

- quality run: pace, then HR fallback;
- easy/long/brick run: HR, then pace fallback;
- quality bike: power, then HR fallback;
- easy/long bike: HR, then power fallback;
- swim: CSS text guidance.

Pace targets are serialized as speed in metres per second through
`targetValueOne` and `targetValueTwo`. HR is sent in bpm and cycling power in
watts. The project pins `garminconnect[workout]>=0.3.6,<0.4`, whose workout
constants include pace, heart-rate and power target types.

## Manual target values

Set these in `.env` when you already know them:

```env
WORKOUT_TARGETS_ENABLED=true
WORKOUT_TARGET_MIN_CONFIDENCE=0.65
RUN_THRESHOLD_PACE=4:30
RUN_THRESHOLD_HR=175
BIKE_FTP_WATTS=250
BIKE_THRESHOLD_HR=168
SWIM_CSS_PACE=2:05
```

The pace units are min/km for running and min/100 m for swimming. Empty values
fall back to accepted benchmark records. Values are examples only.

Inspect the resolved values without contacting Garmin:

```bash
docker compose run --rm triathlon-advisor python advisor.py targets
```

## Two-week Sunday publication

The advisor still runs every morning for recovery adaptation, but automatic
calendar extension happens on Sunday by default:

```env
PLAN_PUBLISH_WEEKDAY=sunday
PLAN_PUBLISH_WEEKS=2
PLAN_PUBLISH_BOOTSTRAP_WHEN_EMPTY=true
```

On Sunday it publishes the next two full Monday-to-Sunday weeks. The first week
of the new publication overlaps the second week from the previous Sunday, so an
unchanged plan normally adds only one new week. An overlapping workout can still
be replaced when its duration, structure, benchmark, or target changed.

On Monday through Saturday the daily process can shorten, replace, or remove
today's already-published workouts, but it does not add a third future week.

The bootstrap option creates an initial two-week window when no active
planner-owned future sessions exist, even when the first startup is not Sunday.

Manual commands are explicit overrides:

```bash
# Preview/publish a 14-day window immediately.
docker compose run --rm triathlon-advisor python advisor.py plan

# Remove future planner-owned entries and recreate a 14-day window immediately.
docker compose run --rm triathlon-advisor python advisor.py rebuild
```

Unrelated Garmin calendar entries are not intentionally removed.

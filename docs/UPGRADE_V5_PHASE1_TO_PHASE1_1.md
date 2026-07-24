# Upgrade V5 Phase 1 to Phase 1.1

Phase 1.1 adds structured workout targets and Sunday-only two-week calendar
publishing. Keep your existing `data/`, `tokens/`, and `models/` directories.

## 1. Stop and back up

```bash
docker compose down
cp -a data "data-backup-$(date +%F-%H%M%S)"
cp -a tokens "tokens-backup-$(date +%F-%H%M%S)"
cp .env ".env-backup-$(date +%F-%H%M%S)"
cp config/plan.yaml "config/plan.yaml-backup-$(date +%F-%H%M%S)"
```

## 2. Merge settings

Use the new `.env.example` and carry over your credentials and existing values.
Add these settings:

```env
PLAN_PUBLISH_WEEKDAY=sunday
PLAN_PUBLISH_WEEKS=2
PLAN_PUBLISH_BOOTSTRAP_WHEN_EMPTY=true

WORKOUT_TARGETS_ENABLED=true
WORKOUT_TARGET_MIN_CONFIDENCE=0.65
RUN_THRESHOLD_PACE=
RUN_THRESHOLD_HR=
BIKE_FTP_WATTS=
BIKE_THRESHOLD_HR=
SWIM_CSS_PACE=
```

Leave manual targets empty to use accepted benchmark results from
`data/training.db`. The included plan keeps swimming on Wednesday, Friday, and
Sunday.

When merging your own `config/plan.yaml`, copy the new `workout_targets` section
and these plan keys:

```yaml
horizon_days: 14
publish_weekday: sunday
publish_weeks: 2
publish_bootstrap_when_empty: true
```

## 3. Build and test

```bash
docker compose build
docker compose run --rm triathlon-advisor python -m unittest discover -v
docker compose run --rm triathlon-advisor python advisor.py measure
```

## 4. Preview calendar changes

Keep writes disabled:

```env
GARMIN_WRITE_ENABLED=false
DRY_RUN=true
```

Then preview:

```bash
docker compose run --rm triathlon-advisor python advisor.py plan
```

The preview shows target ranges and any future entries that would be replaced or
removed. Moving from the old 21-day horizon to two weeks means planner-owned
entries beyond the new window are removed at the next real Sunday publication or
manual rebuild. Unrelated Garmin entries are not intentionally touched.

Existing overlapping planner workouts stay when their fingerprint is unchanged.
They are replaced when their duration, structure, benchmark, or target changed.

## 5. Publish

After reviewing the preview:

```env
GARMIN_WRITE_ENABLED=true
DRY_RUN=false
```

To switch immediately, run:

```bash
docker compose run --rm triathlon-advisor python advisor.py rebuild
```

Otherwise start the service normally. It continues daily recovery checks but
waits until Sunday to extend the future calendar.

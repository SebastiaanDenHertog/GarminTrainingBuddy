# Safe upgrade from V4.1 to V5 Phase 1

## What changes immediately

Copying the new files or rebuilding the Docker image does not modify Garmin.
The `measure` command only reads Garmin and writes the local SQLite database.

Calendar changes occur only when all of these are true:

```env
GARMIN_WRITE_ENABLED=true
DRY_RUN=false
```

and a planning command or daily run is executed.

## Upgrade steps

From the V4.1 folder:

```bash
docker compose down
cp -a data "data-backup-$(date +%F-%H%M%S)"
cp -a tokens "tokens-backup-$(date +%F-%H%M%S)"
cp .env ".env-backup-$(date +%F-%H%M%S)"
```

Extract V5 beside V4.1, then copy persistent state:

```bash
mkdir -p /path/to/v5/data /path/to/v5/tokens /path/to/v5/models
cp -a /path/to/v4/data/. /path/to/v5/data/
cp -a /path/to/v4/tokens/. /path/to/v5/tokens/
```

Create the new environment file and copy your existing secret/config values into
it. Do not replace the new file wholesale because it contains Phase 1 settings:

```bash
cd /path/to/v5
cp .env.example .env
chmod 600 .env
```

Keep calendar writes disabled for the first run:

```env
GARMIN_WRITE_ENABLED=false
DRY_RUN=true
BENCHMARK_AUTO_SCHEDULE=false
```

Build and import history:

```bash
docker compose build
docker compose run --rm triathlon-advisor python advisor.py measure
```

The measurement import does not create, remove or move Garmin workouts.

Start the dashboard and inspect the database, matching and trend panels:

```bash
docker compose up -d triathlon-dashboard
```

Open `http://SERVER_IP:8080`.

## Plan configuration

The included `config/plan.yaml` is Ironman-oriented and restricts swimming to
Wednesday, Friday and Sunday. Compare it with your current file before enabling
writes.

Preview the current 14-day publication window:

```bash
docker compose run --rm triathlon-advisor python advisor.py plan
```

Because writes remain disabled, this is only a preview. Once the schedule is
correct, enable Garmin writes. Use `rebuild` only when you intentionally want to
replace all future planner-managed calendar entries with the new structure.

## Benchmarks

Phase 1 tracks benchmarks immediately, but automatic scheduling is off:

```env
BENCHMARKS_ENABLED=true
BENCHMARK_AUTO_SCHEDULE=false
```

After the initial database import and plan preview, set
`BENCHMARK_AUTO_SCHEDULE=true` to let due tests replace an existing same-sport
session. A benchmark does not add a third session to a day and is not inserted
in a recovery week.

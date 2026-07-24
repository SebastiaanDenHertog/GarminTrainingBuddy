# Garmin Adaptive Triathlon + Strength Advisor V5 Phase 1.1

V5 Phase 1 keeps the training workflow as close as possible to **Garmin + Garmin Connect**:

1. Your Garmin records sleep, HRV, readiness and completed activities.
2. You optionally add five short feedback values to the completed activity's
   **private description** in Garmin Connect.
3. The Docker service reads Garmin, evaluates recovery and training trends,
   validates a weekly progression proposal, and maintains structured swim,
   bike, run and strength workouts on the Garmin calendar.

No separate check-in website is required. V5 Phase 1 adds an optional **read-only local dashboard** for viewing the plan and coach decisions; feedback still stays inside Garmin Connect.

> This is a conservative training-planning tool, not medical care. Pain,
> illness, dizziness, chest symptoms or unusual breathlessness always override
> the automated plan.

## What is new in V5 Phase 1.1

Phase 1 is the measurement foundation needed before allowing AI to make more
aggressive training changes. It adds:

- a local SQLite database at `data/training.db`;
- durable planned-session, activity, recovery and benchmark records;
- scored planned-versus-completed workout matching that uses Garmin workout
  IDs when available, then date, sport, name and duration;
- high-, medium- and low-confidence match counts;
- recent-versus-previous 14-day run, bike and swim performance trends;
- seven-day recovery trend comparisons;
- optional repeatable run, bike and swim benchmark workouts;
- benchmark result storage with a confidence score and extraction method;
- an upgraded read-only dashboard for measurement quality, sport trends and
  benchmark status;
- a `measure` command for the first historical import;
- automatic recent-data refresh during the normal daily advisor run.

Phase 1.1 now uses compatible benchmark results to create structured pace,
heart-rate and cycling-power targets. It never invents a target when the source
data is missing. Manual target values can be supplied in `.env` or
`config/plan.yaml`.

The project still includes the V4 functionality: recovery adaptation, strength
training, multiple sessions per day, Garmin activity-description feedback, the
validated weekly coach and optional embedded Qwen model.

Additional documentation:

- `docs/UPGRADE_V4_TO_V5.md`
- `docs/UPGRADE_V5_PHASE1_TO_PHASE1_1.md`
- `docs/PHASE1_DESIGN.md`
- `docs/TARGETS_AND_WEEKLY_PUBLISHING.md`

## Structured pace, heart-rate and power targets

Garmin supports one primary target on each workout step. V5 Phase 1.1 uses:

- **hard running intervals:** pace range, with HR as a fallback;
- **easy, long and brick runs:** HR range, with pace as a guide or fallback;
- **easy and long rides:** cycling HR range, with power as a fallback;
- **hard cycling intervals:** power range, with HR as a fallback;
- **swimming:** CSS pace in the workout description.

Targets are resolved in this order:

1. non-empty `.env` override;
2. non-empty manual value in `config/plan.yaml`;
3. latest compatible benchmark result in `data/training.db` whose confidence is
   at least `WORKOUT_TARGET_MIN_CONFIDENCE` (default `0.65`).

A 20-minute maximal run pace is slowed by 3% by default before it becomes the
threshold anchor; 20-minute bike power is multiplied by 95% for the initial FTP
estimate. Both factors are configurable. No pace or heart-rate target is guessed. Until a benchmark or manual value is
available, the workout remains RPE-based.

Manual `.env` examples:

```env
WORKOUT_TARGETS_ENABLED=true
WORKOUT_TARGET_MIN_CONFIDENCE=0.65
RUN_THRESHOLD_PACE=4:30
RUN_THRESHOLD_HR=175
BIKE_FTP_WATTS=250
BIKE_THRESHOLD_HR=168
SWIM_CSS_PACE=2:05
```

`RUN_THRESHOLD_PACE` is minutes per kilometre. `SWIM_CSS_PACE` is minutes per
100 metres. The values are examples only; use measured values from your own
benchmarks.

Check exactly which targets V5 resolved before publishing:

```bash
docker compose run --rm triathlon-advisor python advisor.py targets
```

The output includes each value, its source, and benchmark confidence where
available.

The included benchmark schedule now uses:

- `run_threshold_20` for run pace and threshold HR;
- `bike_threshold_20` for cycling power and/or threshold HR;
- `swim_css_400_200` for CSS pace.

A rider without a power meter can still get a cycling HR target from the bike
threshold benchmark. The watch receives numeric target ranges through Garmin's
structured-workout fields. Pace values are sent as speed in metres per second,
which is how Garmin stores pace targets.

## Sunday publishing and a two-week calendar

Automatic daily runs no longer keep adding workouts every morning. The default
configuration is:

```yaml
plan:
  horizon_days: 14
  publish_weekday: sunday
  publish_weeks: 2
  publish_bootstrap_when_empty: true
```

On the first empty-calendar run, V5 creates the initial two-week window. After
that, it publishes automatically only on Sunday. Each Sunday it prepares Monday
through the Sunday two weeks later. Because one week overlaps the previous
publication, it normally adds only one genuinely new week while retaining the
already-published week.

The same behaviour can be configured in `.env` with
`PLAN_PUBLISH_WEEKDAY`, `PLAN_PUBLISH_WEEKS`, and
`PLAN_PUBLISH_BOOTSTRAP_WHEN_EMPTY`.

The daily morning process still evaluates sleep and recovery and may adapt
**today's already-published workout**. It simply does not extend the future
calendar on Monday through Saturday.

Manual commands remain explicit overrides:

```bash
# Preview/publish a 14-day window immediately, even when today is not Sunday.
docker compose run --rm triathlon-advisor python advisor.py plan

# Remove future planner-owned entries and recreate the current 14-day window.
docker compose run --rm triathlon-advisor python advisor.py rebuild
```

## Phase 1 measurement workflow

The data flow is:

```text
Garmin planned workouts + planner_state.json
                 ↓
Garmin completed activities and recovery reports
                 ↓
Scored workout matching
                 ↓
SQLite measurement database
                 ↓
Performance, recovery and benchmark trends
                 ↓
Read-only dashboard and weekly coach input
```

The database contains these main tables:

- `planned_sessions`
- `activities`
- `session_matches`
- `recovery_daily`
- `benchmark_results`
- `analysis_snapshots`

The full private Garmin activity description is still not stored by default.
Only the parsed soreness, fatigue, motivation, pain and RPE values are written
to the database.

### First historical import

After copying your existing `tokens/`, `data/` and configuration, run:

```bash
docker compose run --rm triathlon-advisor python advisor.py measure
```

The default import window is 180 days. Change it in `.env` with:

```env
MEASUREMENT_HISTORY_DAYS=180
```

The normal daily run only refreshes a smaller recent window:

```env
MEASUREMENT_SYNC_ON_DAILY=true
MEASUREMENT_DAILY_SYNC_DAYS=42
```

### Workout matching

A completed activity can be matched to only one planned session. Matching uses:

1. the same Garmin workout ID, when Garmin exposes it;
2. calendar-date distance;
3. sport;
4. workout-name similarity;
5. planned-versus-completed duration similarity.

A one-day tolerance supports workouts completed shortly after midnight or moved
by the athlete. You can make matching stricter in `config/plan.yaml`:

```yaml
measurement:
  match_day_tolerance: 1
  minimum_match_score: 55
```

### Personal benchmarks

The included protocols are:

- `run_aerobic_30`
- `run_threshold_20`
- `bike_aerobic_45`
- `bike_threshold_20`
- `swim_css_400_200`

Benchmarks replace an existing same-sport workout rather than adding an extra
session. They never run during a recovery week. Keep automatic scheduling off
until the first measurement import and dashboard review are complete:

```env
BENCHMARKS_ENABLED=true
BENCHMARK_AUTO_SCHEDULE=false
```

After reviewing the selected days and protocols, enable automatic replacement:

```env
BENCHMARK_AUTO_SCHEDULE=true
```

For a 33-metre pool, the CSS efforts are rounded to complete lengths. The
result extractor prefers matching Garmin laps. If no suitable laps are exposed,
it stores a lower-confidence activity-summary result instead.

## Garmin activity feedback

After a workout, edit the completed activity in Garmin Connect and add this to
its private description:

```text
#coach soreness=3 fatigue=4 motivation=8 pain=no rpe=6
```

The values mean:

- `soreness`: muscle soreness from 0 to 10
- `fatigue`: general fatigue from 0 to 10
- `motivation`: motivation/readiness to train from 0 to 10
- `pain`: `yes` or `no`; use `yes` for pain or a possible injury, not normal
  training discomfort
- `rpe`: how hard the completed session felt, from 1 to 10

A multiline format also works:

```text
Soreness: 3
Fatigue: 4
Motivation: 8
Pain/injury: no
Session RPE: 6
```

Dutch aliases such as `spierpijn`, `vermoeidheid`, `motivatie`, `blessure` and
`inspanning` are also accepted.

Feedback is optional. The advisor continues to use Garmin recovery data when no
feedback is present, but weekly AI progression remains more conservative. By
default, only the parsed values are stored. Free-form private-description text is
removed from the on-disk activity cache.

Show the accepted format from the container:

```bash
docker compose run --rm triathlon-advisor python advisor.py feedback-help
```

## Strength training

V5 Phase 1 treats strength as a planned sport alongside run, bike and swim. The
included Ironman-oriented week contains two 40-minute full-body sessions:

- Tuesday evening after the bike-quality session;
- Friday evening after the endurance swim.

Both are secondary sessions. On an AMBER morning, secondary strength is removed
before the primary endurance workout is changed. On a RED morning, strength is
removed.

Strength minutes use `fixed_minutes` and are **not subtracted from the endurance
weekly target**. The included `base_weekly_minutes: 420` means swim + bike + run;
strength is added separately.

Exercise templates live under `strength_templates` in `config/plan.yaml`:

```yaml
strength_templates:
  full_body_a:
    rounds: 3
    target_rpe: 7
    rest_seconds: 90
    exercises:
      - name: Goblet squat
        reps: "6-8"
      - name: Romanian deadlift
        reps: "6-8"
      - name: Single-arm row
        reps: "8 each side"
```

Schedule a template like this:

```yaml
tuesday:
  - sport: strength
    role: strength
    template: full_body_a
    time: "19:00"
    primary: false
    minimum_gap_hours: 3
    fixed_minutes: 40
    min_minutes: 30
    max_minutes: 50
```

The included plan stays within:

```yaml
plan:
  max_strength_sessions_per_week: 2
```

The exercise list, rounds, RPE target and rest guidance are written into the
Garmin workout description and displayed in the local dashboard. Recovery weeks
shorten the sessions and change them to `strength_maintenance`.

The current Garmin client library does not expose a dedicated typed
`StrengthWorkout` model. V5 Phase 1 therefore uploads a generic Garmin workout
with the strength sport type. Keep Garmin writes disabled until the generated
strength workout has been checked on your own watch and in Garmin Connect.

## Multiple sessions per day

Add multiple entries under a weekday in `config/plan.yaml`.

### Separated double day

The included plan keeps swimming only on Wednesday, Friday and Sunday. For
example, Wednesday contains an easy run and a technique swim:

```yaml
wednesday:
  - sport: run
    role: easy
    time: "10:30"
    primary: false
    weight: 0.75
    min_minutes: 30
    max_minutes: 65

  - sport: swim
    role: technique
    time: "16:30"
    primary: true
    minimum_gap_hours: 4
    weight: 0.75
    min_minutes: 30
    max_minutes: 70
```

### Bike-run brick

```yaml
saturday:
  - sport: bike
    role: long
    time: "08:00"
    primary: true
    weight: 2.80
    min_minutes: 90
    max_minutes: 360

  - sport: run
    role: brick
    primary: false
    after: bike
    max_gap_minutes: 20
    weight: 0.30
    min_minutes: 10
    max_minutes: 45
```

Garmin workout scheduling is date-based. V5 Phase 1 adds the configured time and
relationship to the workout description as guidance, but it does not create a
clock-time calendar event.

The safety limits are configured here:

```yaml
plan:
  max_sessions_per_day: 2
  max_hard_sessions_per_day: 1
  max_hard_sessions_per_week: 2
```

You can set `max_sessions_per_day: 3`, but only one session may remain hard. A
RED morning removes all managed sessions for that day. An AMBER morning removes
secondary sessions first and downgrades the primary demanding session.

## Read-only training dashboard

The dashboard is optional and does not replace Garmin activity feedback. It
reads the planner files and SQLite database under `data/` and shows:

- today's recovery status, Training Readiness and sleep;
- the recommendation and reasons;
- planned-versus-completed match rate and confidence distribution;
- unmatched or uncertain sessions that need review;
- recent run, bike and swim performance trends;
- recovery trends from the latest seven days versus the previous seven;
- personal benchmark due dates and stored results;
- run, bike, swim and strength minutes for the next seven days;
- the rolling two-week schedule, including strength exercises;
- the latest weekly coach explanation;
- embedded-AI state, RAM-check result, shadow mode and Garmin-write status.

Start both services:

```bash
docker compose up -d
```

Open:

```text
http://YOUR_SERVER_IP:8080
```

Change the bind address or host port in `.env`:

```env
DASHBOARD_BIND_ADDRESS=0.0.0.0
DASHBOARD_PORT=8080
```

Use `127.0.0.1` as the bind address when the dashboard should only be reachable through an SSH tunnel or local reverse proxy.

The interface is deliberately read-only: it cannot trigger a plan rebuild,
change configuration, or write to Garmin. It has no login screen, so keep it on
your trusted home/LAN network or place it behind your existing authenticated
reverse proxy. Do not expose port 8080 directly to the public internet.

## Structured training start date

Set the date you began **consistent structured endurance or triathlon training**
in `.env`:

```env
STRUCTURED_TRAINING_START_DATE=2025-06-01
```

Use `YYYY-MM-DD`. Leave it empty when you do not want training age used. The
value is included in analytics and in the facts sent to the local model. It is
background context only: it never overrides current Garmin performance,
recovery, compliance, pain/injury feedback or recent training history.

During the first eight structured-training weeks, V5 Phase 1 caps positive progression
at 2% and blocks added interval intensity. From week eight onward, the normal
performance-and-recovery rules apply. A long training age does not automatically
make the plan harder; measured adaptation still has to support the change.

The analysis labels the context as `foundation` (under 8 weeks), `developing`
(8-23 weeks), `established` (24-51 weeks) or `experienced` (52+ weeks). An invalid
or future date is ignored, logged, and included as a validation warning so the
daily recovery workflow keeps running.

## How weekly progression works

The weekly review compares recent Garmin data with the previous period. It can
return one of four decisions:

- `hold`: keep the current stimulus
- `progress`: make a small volume increase
- `change_stimulus`: hold volume and change one quality variable
- `reduce`: temporarily lower load

The deterministic engine considers:

- matched-session compliance
- run, bike and swim efficiency trends where data is available
- recent hard-session count
- RED and AMBER recovery history
- Garmin activity-description feedback
- whether the current week is a recovery week

Positive weekly volume changes are capped at 5%. The validator rejects a
proposal that simultaneously increases volume and interval intensity, adds load
in a recovery week, exceeds hard-session limits, follows poor compliance, or
follows pain/high-fatigue feedback.

## Why the AI cannot write directly

The model only receives summarized metrics and returns a small JSON proposal.
It never receives Garmin credentials and never calls Garmin. When the embedded
backend is enabled, the model is loaded lazily in the same V5 Phase 1 Python process;
with AI disabled, PyTorch and Transformers are not initialized.

The pipeline is:

```text
Garmin data
  -> deterministic metrics
  -> deterministic reference proposal
  -> optional local-AI proposal
  -> schema, evidence and safety validation
  -> shadow log or approved adjustment
  -> normal planner
  -> Garmin calendar
```

The local evidence catalog is `config/evidence.yaml`. Every proposal must use
known evidence IDs whose allowed decision matches the proposal.

## 1. Install or upgrade from V4.1

Extract V5 Phase 1 beside the current V4.1 project. Copy your existing Garmin tokens so you do
not need to log in again:

```bash
cp -a ../garmin-adaptive-triathlon-advisor-v4/tokens/. ./tokens/
```

Copy `data/` as well so planned-session history can be imported into SQLite:

```bash
cp -a ../garmin-adaptive-triathlon-advisor-v4/data/. ./data/
```

V5 Phase 1 can read the earlier planner state; new session fields have safe defaults.

Then configure:

```bash
cp .env.example .env
mkdir -p tokens data config models
chmod 600 .env
```

On Linux, put the results of these commands into `HOST_UID` and `HOST_GID`:

```bash
id -u
id -g
```

## 2. Login and preview

```bash
docker compose build
docker compose run --rm -it triathlon-advisor python advisor.py login
```

Keep these safe defaults initially:

```env
GARMIN_WRITE_ENABLED=false
DRY_RUN=true
AI_SHADOW_MODE=true
COACH_AUTO_APPLY=false
AI_ENABLED=false
STRUCTURED_TRAINING_START_DATE=
```

Preview the rolling Garmin plan:

```bash
docker compose run --rm triathlon-advisor python advisor.py plan
```

Run the daily recovery check immediately:

```bash
docker compose run --rm triathlon-advisor python advisor.py run
```

## 3. Analyse Garmin history

```bash
docker compose run --rm triathlon-advisor python advisor.py analyze
```

The result is also written under:

```text
data/analytics/YYYY-MM-DD.json
```

Detailed activity summaries are cached under `data/cache/activities/` to reduce
repeated Garmin requests. Description, note and comment fields are redacted before
the cache is written; only the parsed coach values are retained. Activities from
the most recent few days are refreshed so newly edited descriptions can be
discovered.

## 4. Run a weekly coach review

The deterministic review works without an AI model:

```bash
docker compose run --rm triathlon-advisor python advisor.py coach
```

Proposals are stored under:

```text
data/coach/proposals/
```

In shadow mode, `applied` stays false and the Garmin plan remains unchanged.
Review several weeks of proposals before allowing application.

## 5. Enable the model built into V5 Phase 1

The normal Docker image contains the Python, Transformers and PyTorch code needed
to load `Qwen/Qwen3-30B-A3B` directly. The model weights are downloaded on the
first embedded-AI run and cached in `./models/`; they are not stored inside the
project ZIP.

Keep AI off while installing and checking the rest of the Garmin workflow:

```env
AI_ENABLED=false
```

Check the RAM visible to both Linux and the Docker cgroup before enabling it:

```bash
docker compose run --rm triathlon-advisor python advisor.py ai-check
```

The check reports:

- host total and currently available RAM;
- the Docker cgroup memory limit and remaining memory, when one is set;
- the effective available amount, using the smaller usable value;
- an estimated model requirement based on the model name and dtype;
- the configured safety reserve;
- whether model loading is allowed.

The safe embedded configuration is:

```env
AI_ENABLED=true
AI_BACKEND=embedded
AI_MODEL=Qwen/Qwen3-30B-A3B
LOCAL_AI_RAM_CHECK_MODE=strict
LOCAL_AI_REQUIRED_RAM_GB=0
LOCAL_AI_RAM_RESERVE_GB=8
LOCAL_AI_DTYPE=auto
LOCAL_AI_DEVICE_MAP=auto
LOCAL_AI_ENABLE_THINKING=false
AI_SHADOW_MODE=true
COACH_AUTO_APPLY=false
```

`LOCAL_AI_REQUIRED_RAM_GB=0` means V5 Phase 1 calculates a conservative requirement from
the parameter count in the model name. A positive value overrides that estimate.
In `strict` mode, insufficient or unmeasurable RAM prevents model loading and the
weekly review falls back to the deterministic proposal. `warn` mode records the
shortage but still tries to load; `off` disables enforcement. Keep `strict` unless
you have measured the real peak usage yourself.

Run the weekly review:

```bash
docker compose run --rm triathlon-advisor python advisor.py coach
```

The first run can take a long time because it downloads and loads the model. In
the long-running `docker compose up -d` service, the model remains cached in the
Python process after its first use. A new `docker compose run --rm ... coach`
command starts a fresh container and therefore reloads it.

### Optional external backend

The previous Ollama/vLLM route remains available:

```env
AI_ENABLED=true
AI_BACKEND=external
AI_BASE_URL=http://host.docker.internal:11434/v1
AI_MODEL=qwen3:30b-a3b
AI_API_KEY=ollama
```

If model loading, an external endpoint, or JSON parsing fails, V5 Phase 1 records the
error and uses the deterministic proposal as the safe reference. An invalid AI
proposal is never applied.

## 6. Allow validated coach changes

Only after reviewing shadow results:

```env
AI_SHADOW_MODE=false
COACH_AUTO_APPLY=true
```

This allows a validated weekly proposal to write:

```text
data/coach/active_adjustment.json
```

That file can adjust:

- total weekly volume multiplier
- relative run/bike/swim weighting
- quality-session repetition progression

It does **not** write to Garmin by itself. The normal planner still requires:

```env
GARMIN_WRITE_ENABLED=true
DRY_RUN=false
```

Keep the AI switches and Garmin-write switches separate while testing.

After an applied adjustment changes future generated workouts, preview first.
Use `rebuild` only when you intentionally want future V5-managed Garmin calendar
entries recreated:

```bash
docker compose run --rm triathlon-advisor python advisor.py rebuild
```

## 7. Start the advisor and dashboard

```bash
docker compose up -d
docker compose logs -f triathlon-advisor
```

The dashboard is then available at `http://YOUR_SERVER_IP:8080`. Its container
logs are separate:

```bash
docker compose logs -f triathlon-dashboard
```

The daily job:

1. runs the weekly coach review on `AI_REVIEW_DAY` if not already done;
2. reads Garmin recovery and recent private-description feedback;
3. extends the rolling calendar;
4. adapts today's managed workouts after AMBER or RED signals;
5. writes the daily report to `data/reports/`;
6. refreshes recent activities, matching and measurement trends in SQLite.

## Outcome checking

An applied proposal is not automatically considered correct. After at least two
weeks, V5 Phase 1 compares the new analysis with the saved baseline. It marks the
proposal outcome:

- `positive`: efficiency improved without a meaningful compliance/recovery cost
- `negative`: pain, reduced compliance, worse recovery or a material efficiency
  decline appeared
- `inconclusive`: the available data does not show a clear result

This is an audit signal, not proof of causation. Weather, terrain, equipment,
illness and life stress can affect the measurements.

## Commands

```text
python advisor.py login          Interactive Garmin login
python advisor.py run            Daily recovery + planning run
python advisor.py plan           Preview or extend the rolling plan
python advisor.py rebuild        Recreate future V5-managed workouts
python advisor.py status         Show planner and active-coach state
python advisor.py analyze        Build the normal 42-day analysis
python advisor.py measure        Import history and refresh the measurement database
python advisor.py coach          Force a weekly coach proposal
python advisor.py feedback-help  Print the Garmin description format
python advisor.py ai-check       Check RAM before loading embedded AI
python advisor.py daemon         Run the scheduled service
python webapp.py                  Run the read-only dashboard directly
```

## Tests

The feedback parser and coach validator can be tested without Garmin:

```bash
python -m unittest -v test_matching.py test_training_db.py test_benchmarks.py test_analytics.py test_feedback.py test_coach.py test_local_ai.py test_dashboard.py
```

The complete Docker image also includes the planner tests:

```bash
docker compose run --rm triathlon-advisor \
  python -m unittest -v test_planner.py test_targets.py test_matching.py test_training_db.py test_benchmarks.py test_analytics.py test_feedback.py test_coach.py test_local_ai.py test_dashboard.py
```

## Limitations

- `python-garminconnect` uses unofficial Garmin Connect endpoints that Garmin can
  change.
- Strength is currently uploaded through the generic workout endpoint with the
  Garmin strength sport type. Exercise names and rep targets are carried in the
  workout description rather than native per-set watch steps.
- The dashboard has no built-in authentication and should stay on a trusted
  network or behind an authenticated reverse proxy.
- Garmin activity descriptions may have different response field names over
  time. V5 Phase 1 checks several known fields and a conservative recursive fallback.
- Efficiency comparisons need reasonably similar activities and reliable heart
  rate or power data. The validator holds when the data is insufficient.
- Benchmark extraction depends on the laps Garmin exposes. Summary-only results
  remain below the default target confidence threshold and do not drive targets.
- The SQLite match score improves plan-adherence measurement, but a moved or
  renamed manual activity can still require review in the dashboard.
- Phase 1.1 updates targets from accepted benchmarks, but it does not yet learn
  the target percentages from your individual workout-response history.
- Pool workouts are distance-based and require the correct `pool_length_m`.
- V5 Phase 1 does not assess swim technique, diagnose injury or know your full life
  stress.
- The embedded model requires a large download and substantial RAM. The preflight
  estimate is intentionally conservative but cannot guarantee that loading will
  succeed on every combination of hardware, dtype and Transformers version.
- The included Docker build installs CPU PyTorch by default. Accelerator use
  requires a compatible Docker host and a matching `TORCH_INDEX_URL` build
  argument.
- Do not run another service that continuously rewrites the same Garmin calendar
  dates.

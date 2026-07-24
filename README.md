# Garmin Adaptive Triathlon + Strength Advisor V4

V4 keeps the training workflow as close as possible to **Garmin + Garmin Connect**:

1. Your Garmin records sleep, HRV, readiness and completed activities.
2. You optionally add five short feedback values to the completed activity's
   **private description** in Garmin Connect.
3. The Docker service reads Garmin, evaluates recovery and training trends,
   validates a weekly progression proposal, and maintains structured swim,
   bike, run and strength workouts on the Garmin calendar.

No separate check-in website is required. V4 adds an optional **read-only local dashboard** for viewing the plan and coach decisions; feedback still stays inside Garmin Connect.

> This is a conservative training-planning tool, not medical care. Pain,
> illness, dizziness, chest symptoms or unusual breathlessness always override
> the automated plan.

## What is new in V4

- Adds configurable full-body strength sessions and exercise templates.
- Keeps strength minutes separate from the swim/bike/run weekly-volume target.
- Shows recovery, sleep, readiness, training analysis, AI status and the next
  21 days in an optional read-only web dashboard.
- Reads soreness, fatigue, motivation, pain/injury and session RPE from Garmin
  activity descriptions.
- Supports multiple sessions on the same day.
- Distinguishes a primary session from a secondary session.
- Keeps at most one hard session per day and a configurable weekly hard-session
  limit.
- Analyses 42 days of completed Garmin activities.
- Can use an optional structured-training start date as conservative background
  context for weekly progression.
- Estimates planner compliance by matching completed activities to planned
  date, sport and duration.
- Compares recent and previous sport-specific efficiency trends.
- Produces a deterministic weekly progression recommendation.
- Can load `Qwen/Qwen3-30B-A3B` directly inside the V4 Docker service.
- Checks host and Docker/cgroup memory before loading the embedded model.
- Can still use an external OpenAI-compatible Ollama or vLLM endpoint.
- Validates every proposal with hard Python rules and a local evidence catalog.
- Runs in **shadow mode** by default: proposals are recorded but do not alter
  the plan.
- Rechecks applied proposals after at least two weeks and labels their observed
  outcome positive, negative or inconclusive.

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

V4 treats strength as a planned sport alongside run, bike and swim. The default
week contains one 40-minute full-body session on Wednesday after an easy bike.
It is a secondary session, so an AMBER morning removes it before the primary
session is changed.

Strength minutes use `fixed_minutes` and are **not subtracted from the endurance
weekly target**. The default 360-minute target continues to mean swim + bike +
run; strength is added separately.

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
wednesday:
  - sport: strength
    role: strength
    template: full_body_a
    time: "19:00"
    primary: false
    fixed_minutes: 40
    min_minutes: 30
    max_minutes: 50
```

A second weekly session can use `full_body_b`, provided the configured count
stays within:

```yaml
plan:
  max_strength_sessions_per_week: 2
```

The exercise list, rounds, RPE target and rest guidance are written into the
Garmin workout description and displayed in the local dashboard. Recovery weeks
shorten the session and change it to `strength_maintenance`. A RED day removes
strength and schedules only an easy endurance recovery session when one is
appropriate.

The current Garmin client library does not expose a dedicated typed
`StrengthWorkout` model. V4 therefore uploads a generic Garmin workout with the
strength sport type. Keep Garmin writes disabled until the generated strength
workout has been checked on your own watch and Garmin Connect.

## Multiple sessions per day

Add multiple entries under a weekday in `config/plan.yaml`.

### Separated double day

```yaml
tuesday:
  - sport: swim
    role: technique
    time: "06:30"
    primary: false
    minimum_gap_hours: 6
    weight: 0.70
    min_minutes: 30
    max_minutes: 60

  - sport: run
    role: quality
    time: "17:30"
    primary: true
    weight: 0.95
    min_minutes: 40
    max_minutes: 80
```

### Bike-run brick

```yaml
saturday:
  - sport: bike
    role: long
    time: "08:00"
    primary: true
    weight: 1.55
    min_minutes: 75
    max_minutes: 210

  - sport: run
    role: brick
    time: "11:00"
    primary: false
    after: bike
    max_gap_minutes: 30
    weight: 0.35
    min_minutes: 15
    max_minutes: 40
```

Garmin workout scheduling is date-based. V4 adds the configured time and
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
reads the JSON files already written under `data/` and shows:

- today's recovery status, Training Readiness and sleep;
- the recommendation and reasons;
- run, bike, swim and strength minutes for the next seven days;
- the complete rolling 21-day schedule, including strength exercises;
- the latest training analysis and weekly coach explanation;
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

During the first eight structured-training weeks, V4 caps positive progression
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
backend is enabled, the model is loaded lazily in the same V4 Python process;
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

## 1. Install or upgrade from V3

Extract V4 beside the current V3 project. Copy your existing Garmin tokens so you do
not need to log in again:

```bash
cp -a ../garmin-adaptive-triathlon-advisor/tokens ./tokens
```

You may also copy `data/` to preserve planner history. V4 can read earlier planner
state; new session fields have safe defaults.

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

## 5. Enable the model built into V4

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

`LOCAL_AI_REQUIRED_RAM_GB=0` means V4 calculates a conservative requirement from
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

If model loading, an external endpoint, or JSON parsing fails, V4 records the
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
Use `rebuild` only when you intentionally want future V4-managed Garmin calendar
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
5. writes the daily report to `data/reports/`.

## Outcome checking

An applied proposal is not automatically considered correct. After at least two
weeks, V4 compares the new analysis with the saved baseline. It marks the
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
python advisor.py rebuild        Recreate future V4-managed workouts
python advisor.py status         Show planner and active-coach state
python advisor.py analyze        Build the 42-day training analysis
python advisor.py coach          Force a weekly coach proposal
python advisor.py feedback-help  Print the Garmin description format
python advisor.py ai-check       Check RAM before loading embedded AI
python advisor.py daemon         Run the scheduled service
python webapp.py                  Run the read-only dashboard directly
```

## Tests

The feedback parser and coach validator can be tested without Garmin:

```bash
python -m unittest -v test_analytics.py test_feedback.py test_coach.py test_local_ai.py test_dashboard.py
```

The complete Docker image also includes the planner tests:

```bash
docker compose run --rm triathlon-advisor \
  python -m unittest -v test_planner.py test_analytics.py test_feedback.py test_coach.py test_local_ai.py test_dashboard.py
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
  time. V4 checks several known fields and a conservative recursive fallback.
- Efficiency comparisons need reasonably similar activities and reliable heart
  rate or power data. The validator holds when the data is insufficient.
- Pool workouts are distance-based and require the correct `pool_length_m`.
- V4 does not assess swim technique, diagnose injury or know your full life
  stress.
- The embedded model requires a large download and substantial RAM. The preflight
  estimate is intentionally conservative but cannot guarantee that loading will
  succeed on every combination of hardware, dtype and Transformers version.
- The included Docker build installs CPU PyTorch by default. Accelerator use
  requires a compatible Docker host and a matching `TORCH_INDEX_URL` build
  argument.
- Do not run another service that continuously rewrites the same Garmin calendar
  dates.

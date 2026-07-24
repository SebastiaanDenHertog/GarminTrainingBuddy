# Verification notes

The V5 Phase 1.1 source was checked before release with:

```bash
python -m py_compile *.py
node --check web/app.js
python -m unittest -v \
  test_planner.py test_targets.py test_matching.py test_training_db.py \
  test_benchmarks.py test_analytics.py test_feedback.py test_coach.py \
  test_local_ai.py test_dashboard.py
```

The planner tests were run against a small local test double that mirrors the
Garmin 0.3.6 workout-class API, including pace, heart-rate and power target
serialization. In total, 44 offline tests passed.

The release environment did not provide Docker or an installable copy of
`garminconnect[workout]`, so these checks were not performed here:

- a complete `docker compose build`;
- actual Garmin authentication;
- real workout upload, scheduling, unscheduling or benchmark syncing;
- loading the full Qwen model.

Run the following on the target server before enabling Garmin writes:

```bash
docker compose build
docker compose run --rm triathlon-advisor python -m unittest discover -v
docker compose run --rm triathlon-advisor python advisor.py ai-check
docker compose run --rm triathlon-advisor python advisor.py measure
docker compose run --rm triathlon-advisor python advisor.py plan
```

Keep `GARMIN_WRITE_ENABLED=false`, `DRY_RUN=true`, and
`BENCHMARK_AUTO_SCHEDULE=false` during those checks.

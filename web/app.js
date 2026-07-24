const byId = (id) => document.getElementById(id);
const show = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : value;
const minutes = (value) => `${Math.round(Number(value || 0))} min`;
const percent = (value) => value === null || value === undefined ? "—" : `${Math.round(Number(value) * 100)}%`;
const signedPercent = (value) => value === null || value === undefined ? "—" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(1)}%`;
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function statusClass(status) {
  const value = String(status || "unknown").toLowerCase();
  if (["green", "improving", "ready", "initialized"].includes(value)) return "green";
  if (["amber", "stable", "due", "waiting"].includes(value)) return "amber";
  if (["red", "declining", "worsening", "error"].includes(value)) return "red";
  return "";
}

function renderSchedule(sessions) {
  const target = byId("schedule");
  target.innerHTML = "";
  if (!sessions.length) {
    target.innerHTML = '<p class="muted">No generated sessions are available yet. Run <code>python advisor.py plan</code> or the daily advisor.</p>';
    return;
  }
  const groups = new Map();
  sessions.forEach((session) => {
    if (!groups.has(session.day)) groups.set(session.day, []);
    groups.get(session.day).push(session);
  });
  groups.forEach((items, day) => {
    const wrapper = document.createElement("section");
    wrapper.className = "day";
    const date = new Date(`${day}T12:00:00`);
    wrapper.innerHTML = `<div class="day-head"><strong>${escapeHtml(items[0].weekday)}</strong><span class="muted">${escapeHtml(date.toLocaleDateString(undefined, {day:"numeric", month:"short"}))}</span></div>`;
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "workout";
      const benchmark = item.benchmark_id ? `Benchmark · ${item.benchmark_id}` : null;
      const targetText = item.target_summary
        ? `${item.target_summary}${item.target_source ? ` · ${item.target_source}` : ""}`
        : null;
      const strength = item.sport === "strength" && item.strength_exercises?.length
        ? `${item.strength_template || "full body"}: ${item.strength_exercises.map((x) => x.name).join(", ")}`
        : (targetText || benchmark || `${item.role || "session"} · ${item.intensity || "controlled"}`);
      row.innerHTML = `
        <span class="muted">${escapeHtml(show(item.scheduled_time, "Any time"))}</span>
        <span class="sport">${escapeHtml(item.sport)}</span>
        <div class="workout-name"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(strength)}</small></div>
        <span class="duration">${escapeHtml(minutes(item.duration_minutes))}</span>`;
      wrapper.appendChild(row);
    });
    target.appendChild(wrapper);
  });
}

function renderAnalysis(analysis) {
  const target = byId("analysis");
  const rows = [
    ["Period", analysis.period_start && analysis.period_end ? `${analysis.period_start} → ${analysis.period_end}` : "No analysis yet"],
    ["Activities", show(analysis.activity_count)],
    ["Total training", analysis.total_minutes === undefined ? "—" : minutes(analysis.total_minutes)],
    ["Plan compliance", percent(analysis.compliance)],
    ["Unmatched planned sessions", show(analysis.unmatched_planned_sessions)],
    ["Recent hard sessions", show(analysis.hard_sessions_recent_7d)],
    ["Training age", analysis.training_age_weeks === null || analysis.training_age_weeks === undefined ? "—" : `${analysis.training_age_weeks} weeks (${analysis.training_experience_band})`],
  ];
  target.innerHTML = rows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}

function renderTrends(trends) {
  const target = byId("trend-cards");
  const sports = ["run", "bike", "swim"];
  target.innerHTML = sports.map((sport) => {
    const trend = trends?.[sport] || {};
    const status = show(trend.status, "insufficient_data");
    const confidence = trend.confidence === null || trend.confidence === undefined ? "—" : `${Math.round(Number(trend.confidence) * 100)}% confidence`;
    const vo2 = trend.vo2max_change === null || trend.vo2max_change === undefined ? "No VO₂ trend" : `VO₂ ${Number(trend.vo2max_change) >= 0 ? "+" : ""}${Number(trend.vo2max_change).toFixed(1)}`;
    return `<article class="trend-card">
      <div class="section-title"><strong class="sport-title">${escapeHtml(sport)}</strong><span class="chip ${statusClass(status)}">${escapeHtml(String(status).replaceAll("_", " ").toUpperCase())}</span></div>
      <div class="trend-value">${escapeHtml(signedPercent(trend.efficiency_change_percent))}</div>
      <div class="muted">efficiency change</div>
      <dl class="mini-facts">
        <div><dt>Volume</dt><dd>${escapeHtml(signedPercent(trend.volume_change_percent))}</dd></div>
        <div><dt>VO₂</dt><dd>${escapeHtml(vo2)}</dd></div>
        <div><dt>Evidence</dt><dd>${escapeHtml(confidence)}</dd></div>
      </dl>
    </article>`;
  }).join("");
}

function renderMatchingReview(database) {
  const matchedTarget = byId("match-review");
  const unmatchedTarget = byId("unmatched-review");
  const matches = database.review_matches || [];
  const unmatched = database.unmatched_planned || [];
  matchedTarget.innerHTML = matches.length ? matches.map((item) => `
    <div class="review-row">
      <div><strong>${escapeHtml(item.day)} · ${escapeHtml(item.sport)}</strong><small>${escapeHtml(item.planned_name)}</small></div>
      <div class="review-score"><strong>${Math.round(Number(item.score || 0))}/100</strong><small>${escapeHtml(item.method)}</small></div>
    </div>`).join("") : '<p class="muted">No low-confidence matches need review.</p>';
  unmatchedTarget.innerHTML = unmatched.length ? unmatched.map((item) => `
    <div class="review-row">
      <div><strong>${escapeHtml(item.day)} · ${escapeHtml(item.sport)}</strong><small>${escapeHtml(item.name)}</small></div>
      <div class="review-score"><strong>${escapeHtml(minutes(item.duration_minutes))}</strong><small>not matched</small></div>
    </div>`).join("") : '<p class="muted">No unmatched planned sessions in the measurement window.</p>';
}

function renderBenchmarks(items, latest) {
  const target = byId("benchmarks");
  const enabled = items.some((item) => item.enabled);
  const automatic = items.some((item) => item.auto_schedule);
  const chip = byId("benchmark-chip");
  chip.textContent = enabled ? (automatic ? "AUTO" : "READY") : "OFF";
  chip.className = `chip ${enabled ? "green" : ""}`;
  const latestBySport = new Map((latest || []).map((item) => [item.sport, item]));
  if (!items.length) {
    target.innerHTML = '<p class="muted">Run <code>python advisor.py measure</code> to initialise benchmark tracking.</p>';
    return;
  }
  target.innerHTML = items.map((item) => {
    const result = latestBySport.get(item.sport);
    const resultText = result?.result_value !== null && result?.result_value !== undefined
      ? `${Number(result.result_value).toFixed(2)} ${result.result_unit || ""}`
      : "No result yet";
    return `<div class="benchmark-row">
      <div><strong>${escapeHtml(item.sport)}</strong><small>${escapeHtml(item.label || item.protocol)}</small></div>
      <div class="benchmark-result"><strong>${escapeHtml(resultText)}</strong><small>${item.due ? "Due now" : `Next ${item.next_due}`}</small></div>
    </div>`;
  }).join("");
}

function render(data) {
  byId("updated").textContent = `Updated ${new Date(data.generated_at).toLocaleString()}`;
  const recovery = data.recovery || {};
  const status = show(recovery.status, "UNKNOWN");
  byId("recovery-status").textContent = status;
  byId("recovery-day").textContent = show(recovery.day, "No report yet");
  byId("readiness").textContent = show(recovery.training_readiness);
  byId("readiness-level").textContent = show(recovery.training_readiness_level, "Garmin readiness");
  byId("sleep-summary").textContent = recovery.sleep_score === null || recovery.sleep_score === undefined
    ? "No sleep data"
    : `${recovery.sleep_score} score · ${show(recovery.sleep_hours)} hours`;
  byId("weekly-total").textContent = minutes(data.next_7_days?.all);
  byId("weekly-endurance").textContent = `${minutes(data.next_7_days?.endurance)} endurance`;
  byId("recommendation").textContent = show(recovery.recommendation, "Run the advisor to generate the first recovery report.");
  byId("reasons").innerHTML = (recovery.reasons || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
  byId("hrv-ratio").textContent = recovery.hrv_ratio === null || recovery.hrv_ratio === undefined
    ? "—"
    : `${Math.round(Number(recovery.hrv_ratio) * 100)}%`;
  byId("rhr-delta").textContent = recovery.resting_hr_delta === null || recovery.resting_hr_delta === undefined
    ? "—"
    : `${Number(recovery.resting_hr_delta) >= 0 ? "+" : ""}${Number(recovery.resting_hr_delta).toFixed(0)} bpm`;
  const feedback = recovery.subjective_feedback || {};
  const feedbackParts = [];
  if (feedback.max_soreness !== null && feedback.max_soreness !== undefined) feedbackParts.push(`soreness ${feedback.max_soreness}/10`);
  if (feedback.max_fatigue !== null && feedback.max_fatigue !== undefined) feedbackParts.push(`fatigue ${feedback.max_fatigue}/10`);
  if (feedback.min_motivation !== null && feedback.min_motivation !== undefined) feedbackParts.push(`motivation ${feedback.min_motivation}/10`);
  if (feedback.max_session_rpe !== null && feedback.max_session_rpe !== undefined) feedbackParts.push(`RPE ${feedback.max_session_rpe}/10`);
  if (feedback.pain_or_injury_reported === true) feedbackParts.push("pain reported");
  byId("feedback-summary").textContent = feedbackParts.length ? feedbackParts.join(" · ") : "No recent feedback";

  const recoveryChip = byId("recovery-chip");
  recoveryChip.textContent = status;
  recoveryChip.className = `chip ${statusClass(status)}`;

  const measurement = data.measurement || {};
  const database = measurement.database || {};
  const matching = measurement.matching || {};
  const dbReady = Boolean(database.initialized);
  const dbChip = byId("db-chip");
  dbChip.textContent = dbReady ? "READY" : (database.error ? "ERROR" : "WAITING");
  dbChip.className = `chip ${statusClass(dbReady ? "ready" : (database.error ? "error" : "waiting"))}`;
  byId("db-activities").textContent = show(database.activities, 0);
  byId("db-planned").textContent = show(database.planned_sessions, 0);
  byId("db-path").textContent = show(database.path);
  byId("match-rate").textContent = percent(matching.compliance ?? database.match_rate);
  byId("match-detail").textContent = `${show(matching.matched, 0)} of ${show(matching.planned, 0)} planned sessions`;
  const quality = matching.quality || {};
  byId("match-quality").textContent = `${show(quality.high, 0)} high · ${show(quality.medium, 0)} medium · ${show(quality.low, 0)} low`;
  const recoveryTrend = measurement.recovery_trend || {};
  byId("recovery-trend").textContent = show(recoveryTrend.status, "No trend yet");
  renderTrends(measurement.performance_trends || {});
  renderMatchingReview(database);
  renderBenchmarks(measurement.benchmarks || [], database.latest_benchmarks || []);

  const ai = data.ai || {};
  const aiChip = byId("ai-chip");
  aiChip.textContent = ai.enabled ? (ai.shadow_mode ? "SHADOW" : "ON") : "OFF";
  aiChip.className = `chip ${ai.enabled ? "green" : ""}`;
  byId("ai-model").textContent = `${show(ai.model)} (${show(ai.backend)})`;
  const ram = ai.ram || {};
  byId("ai-ram").textContent = ram.message || (ram.error ? `Check failed: ${ram.error}` : "—");

  const coach = data.coach;
  byId("coach-decision").textContent = coach ? `${show(coach.decision).toUpperCase()} · ${Math.round(Number(coach.confidence || 0) * 100)}%` : "No review yet";
  byId("write-status").textContent = data.planner?.garmin_write_enabled && !data.planner?.dry_run ? "Enabled" : "Preview / disabled";
  byId("coach-reasons").innerHTML = coach?.reasoning_summary?.length
    ? coach.reasoning_summary.map((x) => `<li>${escapeHtml(x)}</li>`).join("")
    : "<li>No weekly proposal has been recorded.</li>";

  const totals = data.next_7_days || {};
  byId("sport-totals").innerHTML = ["run", "bike", "swim", "strength"].map((sport) => `<span class="chip">${escapeHtml(sport)}: ${escapeHtml(minutes(totals[sport]))}</span>`).join("");
  renderSchedule(data.upcoming_sessions || []);
  renderAnalysis(data.analysis || {});
}

async function load() {
  byId("refresh").disabled = true;
  byId("error").classList.add("hidden");
  try {
    const response = await fetch("/api/dashboard", {cache: "no-store"});
    if (!response.ok) throw new Error(`Dashboard returned HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    byId("error").textContent = error.message;
    byId("error").classList.remove("hidden");
  } finally {
    byId("refresh").disabled = false;
  }
}

byId("refresh").addEventListener("click", load);
load();
setInterval(load, 60000);

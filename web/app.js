const byId = (id) => document.getElementById(id);
const show = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : value;
const minutes = (value) => `${Math.round(Number(value || 0))} min`;
const percent = (value) => value === null || value === undefined ? "—" : `${Math.round(Number(value) * 100)}%`;
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function statusClass(status) {
  const value = String(status || "unknown").toLowerCase();
  if (["green", "amber", "red"].includes(value)) return value;
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
      const strength = item.sport === "strength" && item.strength_exercises?.length
        ? `${item.strength_template || "full body"}: ${item.strength_exercises.map((x) => x.name).join(", ")}`
        : `${item.role || "session"} · ${item.intensity || "controlled"}`;
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
    ["Recent hard sessions", show(analysis.hard_sessions_recent_7d)],
    ["Training age", analysis.training_age_weeks === null || analysis.training_age_weeks === undefined ? "—" : `${analysis.training_age_weeks} weeks (${analysis.training_experience_band})`],
  ];
  target.innerHTML = rows.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
}

function render(data) {
  byId("updated").textContent = `Updated ${new Date(data.generated_at).toLocaleString()}`;
  const recovery = data.recovery || {};
  const status = show(recovery.status, "UNKNOWN");
  byId("recovery-status").textContent = status;
  byId("recovery-day").textContent = show(recovery.day, "No report yet");
  byId("readiness").textContent = show(recovery.training_readiness);
  byId("readiness-level").textContent = show(recovery.training_readiness_level, "Garmin readiness");
  byId("sleep-score").textContent = show(recovery.sleep_score);
  byId("sleep-hours").textContent = recovery.sleep_hours === null || recovery.sleep_hours === undefined ? "No sleep data" : `${recovery.sleep_hours} hours`;
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

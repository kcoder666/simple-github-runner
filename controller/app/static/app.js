"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let state = null;
let currentTab = localStorageGet("tab") || "overview";
let pollTimer = null;
const filters = { runnerTarget: "", runnerState: "", eventTarget: "", eventLevel: "" };

const SETTING_HELP = {
  reconcile_interval: ["Reconcile interval (s)", "How often desired and actual state are compared."],
  registration_timeout: ["Registration timeout (s)", "A new runner that never comes online in GitHub within this is replaced."],
  offline_grace: ["Offline grace (s)", "How long a runner may drop offline before it is replaced."],
  idle_max_age: ["Idle max age (s)", "Idle runners older than this are recycled. 0 disables."],
  job_timeout: ["Job timeout (s)", "Busy runners older than this are killed as hung. 0 disables."],
  crash_threshold: ["Crash threshold", "Failed starts within the window that trigger spawn backoff."],
  crash_window: ["Crash window (s)", "Window for counting failed starts."],
  spawn_batch: ["Spawn batch", "Max runners started per target per cycle."],
  disk_min_free_pct: ["Min free disk (%)", "Below this, an aggressive prune runs and an alert fires."],
  prune_interval_hours: ["Prune interval (h)", "Scheduled cleanup of build cache and dangling images. 0 disables."],
  prune_unused_images_hours: ["Prune unused images older than (h)", "Container-action images pile up; 0 keeps them."],
  auto_update_runner: ["Auto-update runner", "Rebuild the image when actions/runner ships a new release."],
  recycle_outdated: ["Recycle outdated runners", "Replace idle runners that run an older image."],
  notify_cooldown: ["Alert cooldown (s)", "Minimum time between repeats of the same alert."],
  saturation_alert_minutes: ["Capacity alert after (min)", "Alert when a target has every runner busy at max for this long. 0 disables."],
  credential_expiry_warn_days: ["PAT expiry warning (days)", "Alert when the PAT expires within this many days."],
};

// ---------- utils ----------
function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, (state?.now ?? Date.now() / 1000) - ts);
  return dur(s) + " ago";
}
function dur(s) {
  if (s == null) return "—";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : "—"; }
function bytes(n) {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3500);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("not authenticated"); }
  const text = await res.text();
  let data; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const detail = data?.detail;
    const msg = Array.isArray(detail) ? detail.map(d => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ") : (detail || res.statusText);
    throw new Error(msg);
  }
  return data;
}

// ---------- auth ----------
function showLogin() {
  clearInterval(pollTimer);
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#password").focus();
}
$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { password: $("#password").value } });
    $("#password").value = "";
    start();
  } catch (err) { $("#login-error").textContent = err.message; }
});
$("#logout").addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); showLogin(); });

// ---------- tabs ----------
$$("#tabs button").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));
function switchTab(tab) {
  currentTab = tab; localStorageSet("tab", tab);
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach(s => s.classList.toggle("hidden", s.id !== `tab-${tab}`));
  render();
}

async function start() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  switchTab(currentTab);
  await refresh();
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 5000);
}

async function refresh() {
  try {
    state = await api("/api/state");
    if (currentTab === "events") state.events = await api(eventsUrl());
    render();
  } catch (err) {
    if (err.message !== "not authenticated") $("#heartbeat").outerHTML = `<span id="heartbeat" class="pill err">offline</span>`;
  }
}

// Don't clobber a form the user is typing into.
function editing(section) {
  const a = document.activeElement;
  return a && section.contains(a) && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName);
}

function render() {
  if (!state) return;
  const c = state.controller;
  const hb = $("#heartbeat");
  hb.className = "pill " + (c.healthy ? "ok" : "err");
  hb.textContent = c.healthy ? `reconciled ${ago(c.last_cycle_at)}` : "controller unhealthy";
  const section = $(`#tab-${currentTab}`);
  if (editing(section)) return;
  ({ overview: renderOverview, runners: renderRunners, targets: renderTargets, events: renderEvents, alerts: renderAlerts, system: renderSystem })[currentTab](section);
}

// ---------- health checks ----------
function healthChecks() {
  const checks = [];
  const c = state.controller;
  checks.push(c.healthy
    ? ["ok", "Controller", `Cycle every ${state.settings.reconcile_interval}s · last took ${c.last_cycle_ms ?? "—"} ms`]
    : ["err", "Controller", c.health_detail]);
  if (c.last_cycle_error) checks.push(["err", "Last cycle failed", c.last_cycle_error]);

  const cred = state.github.credentials;
  if (!cred) checks.push(["", "GitHub credentials", "checking…"]);
  else if (!cred.ok) checks.push(["err", "GitHub credentials", cred.error]);
  else {
    let detail = `${cred.mode === "app" ? "GitHub App" : "PAT"}${cred.identity ? " · " + cred.identity : ""}`;
    let level = "ok";
    const exp = state.github.token_expires_at;
    if (exp) {
      const days = (exp - state.now) / 86400;
      detail += ` · expires in ${days.toFixed(0)}d`;
      if (days < state.settings.credential_expiry_warn_days) level = days < 3 ? "err" : "warn";
    } else if (cred.mode === "pat") detail += " · no expiry";
    if (state.github.rate_limit_remaining != null) detail += ` · ${state.github.rate_limit_remaining} API calls left`;
    checks.push([level, "GitHub credentials", detail]);
  }

  const d = state.disk;
  if (d) {
    const level = d.free_pct < state.settings.disk_min_free_pct ? "err" : d.free_pct < state.settings.disk_min_free_pct * 1.5 ? "warn" : "ok";
    checks.push([level, "Docker disk", `${d.free_pct}% free · ${bytes(d.free)} of ${bytes(d.total)}`]);
  }

  const chans = (state.channels || []).filter(ch => ch.enabled);
  const failing = chans.filter(ch => ch.status && !ch.status.ok);
  checks.push(!chans.length ? ["warn", "Alerts", "No notification channel — add one under Alerts"]
    : failing.length ? ["err", "Alerts", `${failing.map(ch => ch.name).join(", ")} failing to deliver`]
    : ["ok", "Alerts", `${chans.length} channel${chans.length > 1 ? "s" : ""}${(state.open_alerts || []).length ? ` · ${state.open_alerts.length} open alert(s)` : " · all quiet"}`]);

  const img = state.image;
  if (img.missing) checks.push(["warn", "Runner image", img.build.building ? "building…" : "missing — will be built"]);
  else {
    const latest = img.latest_runner_version;
    const behind = latest && img.runner_version && latest !== img.runner_version;
    checks.push([behind ? "warn" : "ok", "Runner image",
      `v${img.runner_version || "?"}${latest ? (behind ? ` · v${latest} available` : " · latest") : ""}${img.build.building ? " · rebuilding…" : ""}`]);
  }
  return checks;
}

function renderChecks() {
  return `<div class="checks">${healthChecks().map(([lvl, title, detail]) => `
    <div class="card check"><span class="dot ${lvl}"></span><div><div class="title">${esc(title)}</div><div class="detail">${esc(detail)}</div></div></div>`).join("")}</div>`;
}

// ---------- overview ----------
function renderOverview(el) {
  const runners = state.runners;
  const count = s => runners.filter(r => r.state === s).length;
  const problems = runners.filter(r => ["stale", "stuck", "failed", "orphan"].includes(r.state)).length
    + state.targets.filter(t => t.status?.gh_ok === false || t.status?.backoff_until).length;
  el.innerHTML = `
    ${renderChecks()}
    <div class="grid tiles">
      ${tile("Targets", state.targets.length, `${state.targets.filter(t => t.enabled).length} enabled`)}
      ${tile("Busy", count("busy"), "running jobs")}
      ${tile("Idle", count("idle"), "ready for jobs")}
      ${tile("Starting", count("starting") + count("offline"), "registering")}
      ${tile("Problems", problems, problems ? "self-healing in progress" : "all clear")}
    </div>
    <div class="grid cols-2">
      ${state.targets.map(targetCard).join("") || `<div class="card empty">No targets yet. <a href="#" data-goto="targets">Add a repo or org</a> to start running jobs.</div>`}
    </div>`;
  bindCommon(el);
}
function tile(label, value, sub) {
  return `<div class="card tile"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub)}</div></div>`;
}
function targetCard(t) {
  const st = t.status || {};
  const c = st.counts || {};
  const busy = c.busy || 0, idle = c.idle || 0, starting = (c.starting || 0) + (c.offline || 0);
  const max = Math.max(t.max_runners, busy + idle + starting, 1);
  const pct = n => (100 * n / max).toFixed(1) + "%";
  let alert = "";
  if (!t.enabled) alert = `<div class="alert-line warn">Disabled — draining idle runners, busy jobs finish.</div>`;
  if (st.gh_ok === false) alert = `<div class="alert-line err">GitHub API: ${esc(st.gh_error)}</div>`;
  else if (st.backoff_until) alert = `<div class="alert-line err">Crash-loop backoff — spawning paused for ${dur(st.backoff_until - state.now)}. Check Events for the cause.</div>`;
  else if (st.image_missing) alert = `<div class="alert-line warn">Waiting for the runner image to build.</div>`;
  const bad = ["stale", "stuck", "failed", "orphan"].reduce((n, k) => n + (c[k] || 0), 0);
  return `<div class="card target-card">
    <div class="head">
      <div><div class="name">${esc(t.name)}</div><div class="url">${esc(t.url)}</div></div>
      <span class="pill ${!t.enabled ? "" : st.gh_ok === false || st.backoff_until ? "err" : "ok"}">${!t.enabled ? "disabled" : st.gh_ok === false ? "error" : st.backoff_until ? "backoff" : "healthy"}</span>
    </div>
    <div class="capacity" title="busy / idle / starting of max ${t.max_runners}">
      <span class="busy" style="width:${pct(busy)}"></span><span class="idle" style="width:${pct(idle)}"></span><span class="starting" style="width:${pct(starting)}"></span>
    </div>
    <div class="legend">
      <span><b>${busy}</b> busy</span><span><b>${idle}</b> idle</span><span><b>${starting}</b> starting</span>
      ${bad ? `<span class="s-stale"><b>${bad}</b> recovering</span>` : ""}
      <span>min idle <b>${t.min_idle}</b> · max <b>${t.max_runners}</b></span>
      ${st.demand ? `<span><b>${st.demand}</b> queued</span>` : ""}
    </div>
    <div class="row" style="margin-top:8px">${[...state.controller.base_labels, ...t.labels].map(l => `<span class="tag">${esc(l)}</span>`).join("")}</div>
    ${alert}
    <div class="foot">
      <button class="btn small" data-act="view-runners" data-id="${t.id}">Runners</button>
      <button class="btn small" data-act="edit-target" data-id="${t.id}">Edit</button>
      <button class="btn small" data-act="toggle-target" data-id="${t.id}">${t.enabled ? "Disable" : "Enable"}</button>
      <button class="btn small" data-act="recycle-target" data-id="${t.id}">Recycle idle</button>
      ${st.backoff_until ? `<button class="btn small" data-act="reset-backoff" data-id="${t.id}">Retry now</button>` : ""}
    </div>
  </div>`;
}

// ---------- runners ----------
function renderRunners(el) {
  const rows = state.runners.filter(r =>
    (!filters.runnerTarget || String(r.target_id) === filters.runnerTarget) &&
    (!filters.runnerState || r.state === filters.runnerState));
  const states = [...new Set(state.runners.map(r => r.state))].sort();
  el.innerHTML = `
    <div class="filters">
      <select id="f-rt"><option value="">All targets</option>${state.targets.map(t => `<option value="${t.id}" ${filters.runnerTarget == t.id ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
      <select id="f-rs"><option value="">All states</option>${states.map(s => `<option ${filters.runnerState === s ? "selected" : ""}>${esc(s)}</option>`).join("")}</select>
    </div>
    <div class="card table-wrap" style="padding:0">
      ${rows.length ? `<table>
        <thead><tr><th>Runner</th><th>Target</th><th>State</th><th>Container</th><th>GitHub</th><th>Age</th><th>Busy for</th><th></th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td class="mono">${esc(r.name)}${r.outdated ? ` <span class="pill warn">old image</span>` : ""}</td>
          <td>${esc(r.target_name)}</td>
          <td><b class="s-${esc(r.state)}">${esc(r.state)}</b></td>
          <td>${esc(r.container_status ?? "—")}${r.exit_code ? ` (${esc(r.exit_code)})` : ""}</td>
          <td>${r.gh_status ? esc(r.gh_status) + (r.gh_busy ? " · busy" : "") : "—"}${r.gh_id ? ` <span class="muted">#${esc(r.gh_id)}</span>` : ""}</td>
          <td>${r.started_at || r.created_at ? dur(state.now - (r.started_at || r.created_at)) : "—"}</td>
          <td>${r.busy_since ? dur(state.now - r.busy_since) : "—"}</td>
          <td class="actions">
            ${r.container_status ? `<button class="btn small" data-act="logs" data-name="${esc(r.name)}">Logs</button>` : ""}
            ${r.state === "busy" || r.state === "stuck"
              ? `<button class="btn small danger" data-act="kill" data-name="${esc(r.name)}">Kill job</button>`
              : `<button class="btn small" data-act="recycle" data-name="${esc(r.name)}">Recycle</button>`}
          </td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">No runners${filters.runnerTarget || filters.runnerState ? " match the filters" : ""}.</div>`}
    </div>`;
  $("#f-rt").onchange = e => { filters.runnerTarget = e.target.value; e.target.blur(); render(); };
  $("#f-rs").onchange = e => { filters.runnerState = e.target.value; e.target.blur(); render(); };
  bindCommon(el);
}

// ---------- targets ----------
function renderTargets(el) {
  el.innerHTML = `
    <div class="row spread" style="margin-bottom:12px">
      <p class="muted" style="margin:0">A target is a repo or an org. The controller keeps <b>min idle</b> runners warm and scales up to <b>max</b> as jobs arrive.</p>
      <button class="btn primary" data-act="new-target">Add target</button>
    </div>
    <div class="card table-wrap" style="padding:0">
      ${state.targets.length ? `<table>
        <thead><tr><th>Name</th><th>URL</th><th>Labels</th><th>Min idle</th><th>Max</th><th>Docker</th><th>Limits</th><th>Status</th><th></th></tr></thead>
        <tbody>${state.targets.map(t => `<tr>
          <td><b>${esc(t.name)}</b></td>
          <td class="mono">${esc(t.url)}</td>
          <td>${t.labels.map(l => `<span class="tag">${esc(l)}</span>`).join("") || `<span class="muted">default</span>`}</td>
          <td>${t.min_idle}</td><td>${t.max_runners}</td>
          <td>${t.docker_access ? "socket" : "—"}</td>
          <td class="muted">${[t.cpus ? `${t.cpus} CPU` : "", t.memory || ""].filter(Boolean).join(" · ") || "—"}</td>
          <td>${t.enabled ? `<span class="pill ok">enabled</span>` : `<span class="pill">disabled</span>`}</td>
          <td class="actions">
            <button class="btn small" data-act="edit-target" data-id="${t.id}">Edit</button>
            <button class="btn small" data-act="toggle-target" data-id="${t.id}">${t.enabled ? "Disable" : "Enable"}</button>
            <button class="btn small danger" data-act="delete-target" data-id="${t.id}">Delete</button>
          </td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">No targets yet.</div>`}
    </div>`;
  bindCommon(el);
}

function targetForm(t) {
  const v = t || { name: "", url: "", labels: [], runner_group: "Default", min_idle: 1, max_runners: 4, docker_access: true, cpus: "", memory: "", enabled: true };
  openModal(t ? `Edit ${t.name}` : "Add target", `
    <form id="target-form" class="stack">
      <div class="form-grid">
        <label class="field"><span>Name</span><input name="name" value="${esc(v.name)}" required pattern="[A-Za-z0-9][A-Za-z0-9_.\\-]*" maxlength="40" placeholder="my-app"><small>Used in runner names.</small></label>
        <label class="field"><span>URL</span><input name="url" value="${esc(v.url)}" required placeholder="https://github.com/owner/repo"><small>Repo URL, or org URL for an org-wide pool.</small></label>
        <label class="field wide"><span>Extra labels</span><input name="labels" value="${esc(v.labels.join(", "))}" placeholder="gpu, big"><small>Comma-separated, added to ${esc(state.controller.base_labels.join(", "))}. Jobs must request a subset.</small></label>
        <label class="field"><span>Min idle</span><input name="min_idle" type="number" min="0" max="100" value="${v.min_idle}"><small>Warm runners always waiting. 0 = scale from zero (needs webhook).</small></label>
        <label class="field"><span>Max runners</span><input name="max_runners" type="number" min="0" max="200" value="${v.max_runners}"><small>Hard cap on concurrent runners.</small></label>
        <label class="field"><span>Runner group</span><input name="runner_group" value="${esc(v.runner_group)}"><small>Org targets only.</small></label>
        <label class="field"><span>CPU limit</span><input name="cpus" type="number" step="0.5" min="0" value="${esc(v.cpus ?? "")}" placeholder="unlimited"></label>
        <label class="field"><span>Memory limit</span><input name="memory" value="${esc(v.memory ?? "")}" placeholder="e.g. 8g"></label>
        <label class="check-field"><input type="checkbox" name="docker_access" ${v.docker_access ? "checked" : ""}> Mount host Docker socket</label>
        <label class="check-field"><input type="checkbox" name="enabled" ${v.enabled ? "checked" : ""}> Enabled</label>
      </div>
      <p class="muted" style="font-size:12.5px">The Docker socket lets jobs run container actions, but grants root-equivalent control of the host. Only use it for trusted repos.</p>
      <p id="form-error" class="error-text"></p>
      <div class="row"><button class="btn primary" type="submit">${t ? "Save" : "Create"}</button><button class="btn" type="button" data-close>Cancel</button></div>
    </form>`);
  $("#target-form").addEventListener("submit", async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = {
      name: f.get("name").trim(), url: f.get("url").trim(),
      labels: f.get("labels").split(",").map(s => s.trim()).filter(Boolean),
      runner_group: f.get("runner_group").trim() || "Default",
      min_idle: +f.get("min_idle"), max_runners: +f.get("max_runners"),
      cpus: f.get("cpus") ? +f.get("cpus") : null, memory: f.get("memory").trim() || null,
      docker_access: f.get("docker_access") === "on", enabled: f.get("enabled") === "on",
    };
    try {
      await api(t ? `/api/targets/${t.id}` : "/api/targets", { method: t ? "PUT" : "POST", body });
      closeModal(); toast(t ? "Target saved" : "Target created"); refresh();
    } catch (err) { $("#form-error").textContent = err.message; }
  });
}

// ---------- events ----------
function eventsUrl() {
  const p = new URLSearchParams({ limit: "300" });
  if (filters.eventTarget) p.set("target_id", filters.eventTarget);
  if (filters.eventLevel) p.set("level", filters.eventLevel);
  return `/api/events?${p}`;
}
function renderEvents(el) {
  if (!state.events) { el.innerHTML = `<div class="card empty">Loading…</div>`; refresh(); return; }
  el.innerHTML = `
    <div class="filters">
      <select id="f-et"><option value="">All targets</option>${state.targets.map(t => `<option value="${t.id}" ${filters.eventTarget == t.id ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select>
      <select id="f-el"><option value="">All levels</option>${["info", "warn", "error"].map(l => `<option ${filters.eventLevel === l ? "selected" : ""}>${l}</option>`).join("")}</select>
    </div>
    <div class="card">
      ${state.events.length ? state.events.map(ev => `<div class="event">
        <span class="when" title="${esc(fmtTime(ev.ts))}">${esc(new Date(ev.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</span>
        <span><span class="pill ${ev.level === "error" ? "err" : ev.level === "warn" ? "warn" : "info"}">${esc(ev.kind)}</span></span>
        <span>${esc(ev.message)}${ev.runner ? ` <span class="runner">${esc(ev.runner)}</span>` : ""}</span>
      </div>`).join("") : `<div class="empty">No events.</div>`}
    </div>`;
  $("#f-et").onchange = e => { filters.eventTarget = e.target.value; e.target.blur(); state.events = null; render(); };
  $("#f-el").onchange = e => { filters.eventLevel = e.target.value; e.target.blur(); state.events = null; render(); };
}

// ---------- alerts ----------
const CHANNEL_TYPES = {
  discord: { label: "Discord", fields: [["url", "Webhook URL", "https://discord.com/api/webhooks/…", "Channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL."]] },
  slack: { label: "Slack", fields: [["url", "Webhook URL", "https://hooks.slack.com/services/…", "Create a Slack app with Incoming Webhooks enabled, then add one for the channel."]] },
  telegram: { label: "Telegram", fields: [
    ["bot_token", "Bot token", "123456789:AA…", "Create a bot with @BotFather and add it to the group or channel."],
    ["chat_id", "Chat ID", "-1001234567890", "Group/channel id (or @channelname). Send a message in the chat, then open api.telegram.org/bot<token>/getUpdates to find it."],
    ["thread_id", "Topic ID (optional)", "", "For forum groups: the topic's message_thread_id."]] },
  webhook: { label: "Generic webhook", fields: [["url", "URL", "https://example.com/hooks/runners", "Receives a JSON POST: level, title, message, target, runner, resolved, timestamp."]] },
};
const LEVEL_LABEL = { info: "Everything (info+)", warn: "Warnings and errors", error: "Errors only" };

function renderAlerts(el) {
  const chans = state.channels || [];
  el.innerHTML = `
    <div class="row spread" style="margin-bottom:12px">
      <p class="muted" style="margin:0">Get notified when runners crash-loop, wedge, lose GitHub access, fill the disk, or hit capacity — and again when it clears.</p>
      <button class="btn primary" data-act="new-channel">Add channel</button>
    </div>
    <div class="card table-wrap" style="padding:0">
      ${chans.length ? `<table>
        <thead><tr><th>Name</th><th>Type</th><th>Destination</th><th>Sends</th><th>Last delivery</th><th>Status</th><th></th></tr></thead>
        <tbody>${chans.map(c => `<tr>
          <td><b>${esc(c.name)}</b></td>
          <td>${esc(CHANNEL_TYPES[c.type]?.label || c.type)}</td>
          <td class="mono">${esc(c.config.url || [c.config.chat_id, c.config.thread_id && "topic " + c.config.thread_id].filter(Boolean).join(" · "))}</td>
          <td>${esc(LEVEL_LABEL[c.min_level])}</td>
          <td>${c.status ? `<span class="pill ${c.status.ok ? "ok" : "err"}" title="${esc(c.status.detail)}">${c.status.ok ? "delivered" : "failed"}</span> <span class="muted">${ago(c.status.at)}</span>${c.status.ok ? "" : `<div class="muted" style="font-size:12px">${esc(c.status.detail)}</div>`}` : `<span class="muted">—</span>`}</td>
          <td>${c.enabled ? `<span class="pill ok">on</span>` : `<span class="pill">off</span>`}</td>
          <td class="actions">
            <button class="btn small" data-act="test-channel" data-cid="${c.id}">Send test</button>
            <button class="btn small" data-act="edit-channel" data-cid="${c.id}">Edit</button>
            <button class="btn small danger" data-act="delete-channel" data-cid="${c.id}">Delete</button>
          </td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">No channels yet — nobody hears about problems. Add Discord, Slack, Telegram or a webhook.</div>`}
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card">
        <h2>What triggers an alert</h2>
        <dl class="kv">
          <dt>Error</dt><dd>Crash-loop backoff, runners failing to start, GitHub API/credential failures, disk still low after pruning, image build failures, controller stalls</dd>
          <dt>Warning</dt><dd>Runners replaced (never registered, went offline, hung job, crashed/OOM), low disk, PAT expiring, capacity saturated</dd>
          <dt>Info</dt><dd>Controller restarts</dd>
          <dt>Resolved</dt><dd>Sent once when an alerted problem clears, to the same channels that got the alert</dd>
          <dt>Repeats</dt><dd>The same alert is sent at most once per ${dur(state.settings.notify_cooldown)} (Settings → Alert cooldown)</dd>
        </dl>
      </div>
      <div class="card">
        <h2>Open alerts</h2>
        ${(state.open_alerts || []).length ? `<div>${state.open_alerts.map(k => `<span class="tag">${esc(k)}</span>`).join(" ")}</div>
          <p class="muted" style="font-size:12.5px">Each will send a “Resolved” message when it clears.</p>` : `<p class="muted">None — all quiet.</p>`}
        ${state.public_url ? "" : `<p class="muted" style="font-size:12.5px">Tip: set <span class="mono">PUBLIC_URL</span> in .env to link alerts to this dashboard.</p>`}
      </div>
    </div>`;
  bindCommon(el);
}

function channelForm(c) {
  const type = c?.type || "discord";
  openModal(c ? `Edit ${c.name}` : "Add notification channel", `
    <form id="channel-form" class="stack">
      <div class="form-grid">
        <label class="field"><span>Name</span><input name="name" value="${esc(c?.name || "")}" required maxlength="60" placeholder="#ci-alerts"></label>
        <label class="field"><span>Type</span><select name="type">${Object.entries(CHANNEL_TYPES).map(([k, v]) => `<option value="${k}" ${k === type ? "selected" : ""}>${v.label}</option>`).join("")}</select></label>
        <div id="channel-fields" class="wide form-grid"></div>
        <label class="field"><span>Send</span><select name="min_level">${Object.entries(LEVEL_LABEL).map(([k, v]) => `<option value="${k}" ${k === (c?.min_level || "warn") ? "selected" : ""}>${v}</option>`).join("")}</select></label>
        <label class="check-field"><input type="checkbox" name="enabled" ${c?.enabled === false ? "" : "checked"}> Enabled</label>
      </div>
      ${c ? `<p class="muted" style="font-size:12.5px">Secrets are shown masked; leave them unchanged to keep the stored value.</p>` : ""}
      <p id="form-error" class="error-text"></p>
      <div class="row"><button class="btn primary" type="submit">${c ? "Save" : "Add"}</button>${c ? "" : `<button class="btn" type="submit" data-test>Add &amp; send test</button>`}<button class="btn" type="button" data-close>Cancel</button></div>
    </form>`);
  const form = $("#channel-form");
  const drawFields = t => {
    $("#channel-fields").innerHTML = CHANNEL_TYPES[t].fields.map(([k, label, ph, help]) =>
      `<label class="field ${k === "url" ? "wide" : ""}"><span>${esc(label)}</span><input name="cfg_${k}" value="${esc(c && c.type === t ? c.config[k] || "" : "")}" placeholder="${esc(ph)}" ${k === "thread_id" ? "" : "required"} autocomplete="off"><small>${esc(help)}</small></label>`).join("");
  };
  drawFields(type);
  form.elements.type.onchange = e => drawFields(e.target.value);
  let sendTest = false;
  $$("button[type=submit]", form).forEach(b => b.onclick = () => { sendTest = b.hasAttribute("data-test"); });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const f = new FormData(form), t = f.get("type"), config = {};
    for (const [k] of CHANNEL_TYPES[t].fields) config[k] = (f.get("cfg_" + k) || "").trim();
    const body = { name: f.get("name").trim(), type: t, config, min_level: f.get("min_level"), enabled: f.get("enabled") === "on" };
    try {
      const saved = await api(c ? `/api/channels/${c.id}` : "/api/channels", { method: c ? "PUT" : "POST", body });
      closeModal();
      if (sendTest) {
        const r = await api(`/api/channels/${saved.id}/test`, { method: "POST" });
        toast(r.ok ? "Channel added — test message delivered" : `Added, but the test failed: ${r.detail}`, !r.ok);
      } else toast(c ? "Channel saved" : "Channel added");
      refresh();
    } catch (err) { $("#form-error").textContent = err.message; }
  });
}

// ---------- system ----------
function renderSystem(el) {
  const c = state.controller, img = state.image, d = state.disk, cred = state.github.credentials || {};
  const usedPct = d ? 100 - d.free_pct : 0;
  const meterCls = d && d.free_pct < state.settings.disk_min_free_pct ? "err" : d && d.free_pct < state.settings.disk_min_free_pct * 1.5 ? "warn" : "";
  const report = state.last_prune_report;
  el.innerHTML = `
    ${renderChecks()}
    <div class="grid cols-2">
      <div class="card">
        <div class="row spread"><h2>GitHub</h2><button class="btn small" data-act="check-creds">Check now</button></div>
        <dl class="kv">
          <dt>Auth</dt><dd>${c.auth_mode === "app" ? "GitHub App" : c.auth_mode === "pat" ? "Personal access token" : "<b class='s-stale'>not configured</b>"}</dd>
          <dt>Identity</dt><dd>${esc(cred.identity || "—")}</dd>
          <dt>Status</dt><dd>${cred.ok ? `<span class="pill ok">valid</span>` : cred.error ? `<span class="pill err">${esc(cred.error)}</span>` : "—"}</dd>
          <dt>Expires</dt><dd>${state.github.token_expires_at ? `${fmtTime(state.github.token_expires_at)} (${dur(state.github.token_expires_at - state.now)})` : c.auth_mode === "app" ? "never (tokens minted on demand)" : "no expiry reported"}</dd>
          <dt>Rate limit</dt><dd>${state.github.rate_limit_remaining ?? "—"} remaining${state.github.rate_limit_reset ? `, resets ${fmtTime(state.github.rate_limit_reset)}` : ""}</dd>
          <dt>Webhook</dt><dd>${c.webhook_enabled ? `enabled at <span class="mono">${esc(location.origin)}/webhook/github</span> (event: Workflow jobs)` : "disabled — set GITHUB_WEBHOOK_SECRET for instant scale-up"}</dd>
          <dt>Base labels</dt><dd>${c.base_labels.map(l => `<span class="tag">${esc(l)}</span>`).join("")}</dd>
        </dl>
      </div>
      <div class="card">
        <div class="row spread"><h2>Runner image</h2><button class="btn small" data-act="rebuild" ${img.build.building ? "disabled" : ""}>${img.build.building ? "Building…" : "Rebuild now"}</button></div>
        <dl class="kv">
          <dt>Image</dt><dd class="mono">${esc(img.tag || "—")}</dd>
          <dt>Runner version</dt><dd>${img.missing ? "missing" : "v" + esc(img.runner_version || "?")}</dd>
          <dt>Latest release</dt><dd>${img.latest_runner_version ? "v" + esc(img.latest_runner_version) : "—"}</dd>
          <dt>Auto-update</dt><dd>${state.settings.auto_update_runner ? "on" : "off"}</dd>
          <dt>Size</dt><dd>${bytes(img.size)}</dd>
          <dt>Last build</dt><dd>${img.build.finished_at ? `${esc(img.build.last_result)} · ${ago(img.build.finished_at)}` : "—"}${img.build.log_tail ? ` · <a href="#" data-act="build-log">log</a>` : ""}</dd>
        </dl>
      </div>
      <div class="card">
        <div class="row spread"><h2>Disk</h2>
          <div class="row"><button class="btn small" data-act="prune">Prune</button><button class="btn small danger" data-act="prune-aggressive">Aggressive prune</button></div></div>
        ${d ? `<div class="meter"><span class="${meterCls}" style="width:${usedPct}%"></span></div>
          <dl class="kv">
            <dt>Free</dt><dd>${bytes(d.free)} of ${bytes(d.total)} (${d.free_pct}%)</dd>
            <dt>Last prune</dt><dd>${state.last_prune ? ago(state.last_prune) : "—"}${report ? ` · reclaimed ${bytes(report.total_reclaimed)}` : ""}</dd>
            <dt>Schedule</dt><dd>${state.settings.prune_interval_hours ? `every ${state.settings.prune_interval_hours}h` : "off"}; unused images older than ${state.settings.prune_unused_images_hours || "∞"}h</dd>
          </dl>` : `<p class="muted">Checking…</p>`}
      </div>
      <div class="card">
        <h2>Controller</h2>
        <dl class="kv">
          <dt>Health</dt><dd>${c.healthy ? `<span class="pill ok">healthy</span>` : `<span class="pill err">${esc(c.health_detail)}</span>`}</dd>
          <dt>Up since</dt><dd>${fmtTime(c.started_at)} (${dur(state.now - c.started_at)})</dd>
          <dt>Last cycle</dt><dd>${ago(c.last_cycle_at)} · ${c.last_cycle_ms ?? "—"} ms</dd>
          <dt>Name prefix</dt><dd class="mono">${esc(c.name_prefix)}</dd>
          <dt>Endpoints</dt><dd class="mono">/healthz · /metrics</dd>
        </dl>
        <div class="row" style="margin-top:12px"><button class="btn small" data-act="reconcile">Reconcile now</button></div>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Settings</h2>
      <form id="settings-form" class="stack">
        <div class="form-grid">${Object.entries(SETTING_HELP).map(([k, [label, help]]) => {
          const val = state.settings[k];
          if (typeof val === "boolean") return `<label class="check-field"><input type="checkbox" name="${k}" ${val ? "checked" : ""}> ${esc(label)}</label>`;
          return `<label class="field ${k === "notify_webhook_url" ? "wide" : ""}"><span>${esc(label)}</span><input name="${k}" ${typeof val === "number" ? `type="number" min="0"` : ""} value="${esc(val)}"><small>${esc(help)}</small></label>`;
        }).join("")}</div>
        <p id="settings-error" class="error-text"></p>
        <div class="row"><button class="btn primary" type="submit">Save settings</button></div>
      </form>
    </div>`;
  $("#settings-form").addEventListener("submit", async e => {
    e.preventDefault();
    const values = {};
    for (const k of Object.keys(SETTING_HELP)) {
      const input = e.target.elements[k];
      values[k] = input.type === "checkbox" ? input.checked : input.type === "number" ? +input.value : input.value;
    }
    try { await api("/api/settings", { method: "PUT", body: { values } }); toast("Settings saved"); document.activeElement.blur(); refresh(); }
    catch (err) { $("#settings-error").textContent = err.message; }
  });
  bindCommon(el);
}

// ---------- actions ----------
function bindCommon(el) {
  $$("[data-goto]", el).forEach(a => a.onclick = e => { e.preventDefault(); switchTab(a.dataset.goto); });
  $$("[data-act]", el).forEach(b => b.onclick = e => { e.preventDefault(); act(b.dataset.act, b.dataset); });
}

async function act(action, data) {
  const target = data.id ? state.targets.find(t => t.id == data.id) : null;
  try {
    switch (action) {
      case "new-target": return targetForm(null);
      case "new-channel": return channelForm(null);
      case "edit-channel": return channelForm(state.channels.find(c => c.id == data.cid));
      case "test-channel": {
        const r = await api(`/api/channels/${data.cid}/test`, { method: "POST" });
        toast(r.ok ? "Test message delivered" : `Test failed: ${r.detail}`, !r.ok); break;
      }
      case "delete-channel": {
        const c = state.channels.find(x => x.id == data.cid);
        if (!confirm(`Delete notification channel "${c.name}"?`)) return;
        await api(`/api/channels/${c.id}`, { method: "DELETE" }); toast("Channel deleted"); break;
      }
      case "edit-target": return targetForm(target);
      case "view-runners": filters.runnerTarget = data.id; filters.runnerState = ""; return switchTab("runners");
      case "toggle-target":
        await api(`/api/targets/${target.id}`, { method: "PUT", body: { ...target, enabled: !target.enabled } });
        toast(target.enabled ? "Disabled — draining" : "Enabled"); break;
      case "delete-target":
        if (!confirm(`Delete ${target.name}? Its runners are removed immediately and running jobs are cancelled.`)) return;
        await api(`/api/targets/${target.id}`, { method: "DELETE" }); toast("Target deleted"); break;
      case "recycle-target": {
        const r = await api(`/api/targets/${target.id}/recycle`, { method: "POST" });
        toast(`Recycled ${Object.keys(r.results).length} runner(s)`); break;
      }
      case "reset-backoff": await api(`/api/targets/${target.id}/reset-backoff`, { method: "POST" }); toast("Retrying"); break;
      case "recycle": {
        const r = await api(`/api/runners/${encodeURIComponent(data.name)}/recycle`, { method: "POST" });
        toast(r.result, r.result !== "recycled"); break;
      }
      case "kill":
        if (!confirm(`Kill ${data.name}? Its running job will fail.`)) return;
        await api(`/api/runners/${encodeURIComponent(data.name)}/recycle?force=true`, { method: "POST" }); toast("Runner killed"); break;
      case "logs": return showLogs(data.name);
      case "build-log": return openModal("Last image build", `<pre class="log">${esc(state.image.build.log_tail)}</pre>`);
      case "rebuild": await api("/api/system/rebuild-image", { method: "POST" }); toast("Image build started"); break;
      case "prune": await api("/api/system/prune", { method: "POST" }); toast("Prune started"); break;
      case "prune-aggressive":
        if (!confirm("Aggressive prune removes ALL build cache and unused images older than 24h on this Docker host. Continue?")) return;
        await api("/api/system/prune?aggressive=true", { method: "POST" }); toast("Aggressive prune started"); break;
      case "reconcile": await api("/api/system/reconcile", { method: "POST" }); toast("Reconcile triggered"); break;
      case "check-creds": {
        const r = await api("/api/system/check-credentials", { method: "POST" });
        toast(r.ok ? "Credentials valid" : `Credentials invalid: ${r.error}`, !r.ok); break;
      }
    }
    setTimeout(refresh, 400);
  } catch (err) { toast(err.message, true); }
}

async function showLogs(name) {
  openModal(`Logs · ${name}`, `<pre class="log">Loading…</pre>`);
  try {
    const res = await fetch(`/api/runners/${encodeURIComponent(name)}/logs?tail=600`);
    const text = await res.text();
    const pre = $("#modal-body pre");
    pre.textContent = text || "(no output)";
    pre.scrollTop = pre.scrollHeight;
  } catch (err) { $("#modal-body pre").textContent = err.message; }
}

// ---------- modal ----------
function openModal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $$("[data-close]", $("#modal-body")).forEach(b => b.onclick = closeModal);
  $("#modal").showModal();
}
function closeModal() { $("#modal").close(); }
$("#modal-close").onclick = closeModal;

// ---------- boot ----------
api("/api/state").then(s => { state = s; start(); }).catch(() => showLogin());

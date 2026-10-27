"use strict";

// BackupBox web UI: polls /api/state every second and renders it.
// Cards are rebuilt only when their structure changes; live numbers
// (progress, speed, countdown) are patched in place so buttons stay tappable.

const POLL_MS = 1000;
const HIDDEN_POLL_MS = 5000;

let state = null;
let lastNoticeId = null;
let pollTimer = null;
let connectionLost = false;
let settingsDirty = false;

const $ = (sel, root = document) => root.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function fmtBytes(n) {
  if (n == null) return "–";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let v = Number(n);
  let i = 0;
  while (Math.abs(v) >= 1000 && i < units.length - 1) { v /= 1000; i += 1; }
  return i === 0 ? `${v} o` : `${v.toLocaleString("fr-CA", { maximumFractionDigits: 1 })} ${units[i]}`;
}

function fmtDuration(s) {
  if (s == null) return "–";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min ${String(sec).padStart(2, "0")} s`;
  return `${sec} s`;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("fr-CA", { dateStyle: "short", timeStyle: "short" });
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join(" ; ")
      : `Erreur ${res.status}`;
    throw new Error(msg);
  }
  return data;
}

async function act(method, url, body) {
  try {
    const result = await api(method, url, body);
    await refresh();
    return result;
  } catch (err) {
    toast(err.message, "error");
    await refresh();
    throw err;
  }
}

function toast(text, level = "info") {
  const el = document.createElement("div");
  el.className = `toast ${level}`;
  el.textContent = text;
  const box = $("#toasts");
  box.append(el);
  while (box.children.length > 2) box.firstElementChild.remove();
  setTimeout(() => el.remove(), level === "error" ? 8000 : 4000);
}

// ------------------------------------------------------------------ polling

async function refresh() {
  try {
    state = await api("GET", "/api/state");
    connectionLost = false;
    render();
  } catch {
    connectionLost = true;
    renderBanners();
  }
}

function schedule() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await refresh();
    schedule();
  }, document.hidden ? HIDDEN_POLL_MS : POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
  schedule();
});

// ----------------------------------------------------------------- helpers

const JOB_LABELS = {
  waiting: "En attente",
  held: "En pause",
  countdown: "Démarrage imminent",
  queued: "En file d'attente",
  running: "Sauvegarde en cours",
  done: "Sauvegardée et vérifiée",
  partial: "Sauvegarde incomplète",
  failed: "Échec",
  cancelled: "Annulée",
};

function jobTone(job) {
  if (!job) return "";
  if (job.state === "done") return "ok";
  if (job.state === "partial" || job.state === "failed") return "err";
  if (job.state === "cancelled") return "warn";
  return "busy";
}

function jobsByDevice() {
  const map = new Map();
  for (const job of state.jobs) map.set(job.device_key, job);
  return map;
}

function destination() {
  return state.devices.find((d) => d.key === state.active_destination) || null;
}

// ------------------------------------------------------------------ render

function render() {
  renderStatus();
  renderBanners();
  renderDestination();
  renderCards();
  renderSettings();
  renderSystem();
  renderNotices();
}

function renderStatus() {
  const chip = $("#status-chip");
  const running = state.jobs.some((j) => ["running", "queued", "countdown"].includes(j.state));
  const failed = state.jobs.some((j) => ["failed", "partial"].includes(j.state));
  let text = "Prête";
  let tone = "ok";
  if (running) { text = "Sauvegarde…"; tone = "busy"; }
  else if (failed) { text = "Attention"; tone = "err"; }
  else if (!destination()) { text = "SSD absent"; tone = "warn"; }
  chip.textContent = text;
  chip.className = `chip ${tone}`;
}

function renderBanners() {
  const out = [];
  if (connectionLost) out.push(`<div class="banner err">Connexion à BackupBox perdue. Vérifiez le Wi-Fi « BackupBox ».</div>`);
  if (state) {
    const power = state.health && state.health.power;
    if (power && power.available && power.under_voltage_now) {
      out.push(`<div class="banner err">Alimentation insuffisante en ce moment : les disques USB peuvent se déconnecter. Utilisez un bloc 5 V / 3 A.</div>`);
    } else if (power && power.available && power.under_voltage_since_boot) {
      out.push(`<div class="banner warn">Sous-tension détectée depuis le démarrage : vérifiez l'alimentation et le câble.</div>`);
    }
    if (state.simulate) out.push(`<div class="banner info">Mode simulation : aucun vrai disque n'est utilisé.</div>`);
  }
  const html = out.join("");
  const el = $("#banners");
  if (el.innerHTML !== html) el.innerHTML = html;
}

function renderDestination() {
  const el = $("#destination");
  const dests = state.devices.filter((d) => d.role === "destination" && d.status === "ready");
  const dest = destination();
  let html;
  if (!dest) {
    html = `<p class="empty">Branchez le SSD de destination. Un disque jamais utilisé apparaît dans « Cartes » : choisissez « Utiliser comme SSD de destination ».</p>`;
  } else {
    const project = dest.project;
    const projectText = project === null || project === undefined
      ? `<span class="state-line warn">Aucun projet choisi</span>`
      : `<strong>${esc(project === "" ? "(racine du SSD)" : project)}</strong>`;
    const used = dest.total_bytes ? dest.total_bytes - dest.free_bytes : 0;
    html = `
      <div class="card ${project == null ? "warn" : "ok"}" data-sig="dest">
        <div class="card-head">
          <span class="card-title">${esc(dest.name)}</span>
          <span class="muted small">${esc(dest.fstype || "")}</span>
        </div>
        <div>Projet : ${projectText}</div>
        <meter min="0" max="${dest.total_bytes || 1}" value="${used}" high="${(dest.total_bytes || 1) * 0.9}" aria-label="Espace utilisé"></meter>
        <div class="muted small">${fmtBytes(dest.free_bytes)} libres sur ${fmtBytes(dest.total_bytes)}</div>
        ${dests.length > 1 ? `<label class="field"><span>SSD actif</span><select data-action="select-dest">${dests.map((d) => `<option value="${esc(d.key)}" ${d.key === dest.key ? "selected" : ""}>${esc(d.name)}</option>`).join("")}</select></label>` : ""}
        <div class="actions">
          <button type="button" class="btn ${project == null ? "primary" : ""}" data-action="choose-project">${project == null ? "Choisir le projet" : "Changer de projet"}</button>
          <button type="button" class="btn" data-action="eject" data-key="${esc(dest.key)}">Éjecter le SSD</button>
        </div>
      </div>`;
  }
  if (el.dataset.html !== html) {
    el.innerHTML = html;
    el.dataset.html = html;
  }
}

function deviceView(dev, job) {
  // Returns {sig, html, live}: sig changes => rebuild; live => patched each tick.
  const brand = dev.card && dev.card.brand;
  const heading = esc(brand && brand.toLowerCase() !== String(dev.name).toLowerCase() ? `${brand} · ${dev.name}` : dev.name);
  const meta = `${fmtBytes(dev.size)} · ${esc(dev.fstype || "?")}${dev.read_only ? " · lecture seule" : ""}`;
  let tone = "";
  let body = "";
  let actions = [];
  const live = {};

  if (dev.status === "mounting") {
    body = `<div class="muted">Montage en cours…</div>`;
  } else if (dev.status === "ejected") {
    tone = job && job.state === "done" ? "ok" : "";
    body = job && job.state === "done"
      ? `<div class="state-line ok">✓ Sauvegardée et vérifiée. Vous pouvez retirer la carte et la formater dans l'appareil.</div><div class="muted small">${esc(job.message || "")}</div>`
      : `<div class="state-line">Éjectée : vous pouvez la retirer.</div>`;
    actions.push(`<button type="button" class="btn" data-action="mount" data-key="${esc(dev.key)}">Remonter</button>`);
    if (job) actions.push(`<button type="button" class="btn" data-action="details" data-job="${esc(job.id)}">Détails</button>`);
  } else if (dev.status === "error") {
    tone = "err";
    body = `<div class="state-line err">${esc(dev.error || "Erreur")}</div>`;
    actions.push(`<button type="button" class="btn" data-action="mount" data-key="${esc(dev.key)}">Réessayer</button>`);
  } else if (job && ["waiting", "held", "countdown", "queued", "running"].includes(job.state)) {
    tone = "busy";
    const src = `<code>${esc(job.source_name)}</code>`;
    if (job.state === "running") {
      body = `
        <div class="state-line">Sauvegarde vers ${src}${job.project ? ` · ${esc(job.project)}` : ""}</div>
        <progress max="100" value="0" data-live="bar" aria-label="Progression"></progress>
        <div class="stats"><span data-live="percent"></span><span data-live="files"></span><span data-live="speed"></span><span data-live="eta"></span></div>
        <div class="current" data-live="current"></div>`;
      const p = job.progress;
      live.bar = p.percent;
      live.percent = `${p.percent.toLocaleString("fr-CA")} %`;
      live.files = `${p.files_done} / ${p.files_total || "?"} fichiers`;
      live.speed = p.speed_bps ? `${fmtBytes(p.speed_bps)}/s` : "";
      live.eta = p.eta_s != null ? `reste ≈ ${fmtDuration(p.eta_s)}` : "";
      live.current = p.current_file ? `${p.current_step || p.phase} : ${p.current_file}` : p.phase;
      actions.push(`<button type="button" class="btn danger" data-action="cancel" data-job="${esc(job.id)}">Annuler</button>`);
    } else {
      let line = "";
      if (job.state === "countdown") line = `Sauvegarde vers ${src} dans <span data-live="countdown"></span>`;
      else if (job.state === "queued") line = `En file d'attente vers ${src}`;
      else if (job.state === "held") line = `En pause : vérifiez la source ${src}`;
      else line = esc(job.wait_reason || "En attente");
      body = `<div class="state-line">${line}</div>`;
      if (!job.known_card && job.state !== "waiting") {
        body += `<div class="muted small">Carte jamais vue sur ce SSD. Si elle vient d'un autre appareil, changez la source.</div>`;
      }
      live.countdown = job.countdown_s != null ? `${job.countdown_s} s` : "";
      if (job.state !== "queued") {
        if (job.state !== "waiting") actions.push(`<button type="button" class="btn primary" data-action="start" data-job="${esc(job.id)}">Démarrer maintenant</button>`);
        actions.push(`<button type="button" class="btn" data-action="source" data-job="${esc(job.id)}">Changer la source</button>`);
      }
      actions.push(`<button type="button" class="btn danger" data-action="cancel" data-job="${esc(job.id)}">Annuler</button>`);
    }
  } else if (job) {
    tone = jobTone(job);
    const label = JOB_LABELS[job.state] || job.state;
    body = `<div class="state-line ${tone}">${job.state === "done" ? "✓ " : ""}${esc(label)} → <code>${esc(job.source_name)}</code></div>`;
    if (job.message) body += `<div>${esc(job.message)}</div>`;
    if (job.error) body += `<div class="state-line err">${esc(job.error)}</div>`;
    if (job.state === "done") body += `<div class="muted small">Vous pouvez éjecter la carte et la formater dans l'appareil.</div>`;
    if (job.state !== "done") actions.push(`<button type="button" class="btn primary" data-action="backup" data-key="${esc(dev.key)}">Relancer</button>`);
    actions.push(`<button type="button" class="btn" data-action="details" data-job="${esc(job.id)}">Détails</button>`);
    actions.push(`<button type="button" class="btn" data-action="eject" data-key="${esc(dev.key)}">Éjecter</button>`);
  } else if (dev.role === "source") {
    body = `<div>Carte d'appareil photo (${esc(dev.card.default_source)}). Sauvegarde automatique désactivée.</div>`;
    actions.push(`<button type="button" class="btn primary" data-action="backup" data-key="${esc(dev.key)}">Sauvegarder</button>`);
    actions.push(`<button type="button" class="btn" data-action="eject" data-key="${esc(dev.key)}">Éjecter</button>`);
  } else if (dev.role === "unknown") {
    tone = "warn";
    body = `<div>Disque non reconnu : pas de dossier <code>DCIM</code>, il n'est donc pas sauvegardé automatiquement.</div>`;
    actions.push(`<button type="button" class="btn primary" data-action="init" data-key="${esc(dev.key)}">Utiliser comme SSD de destination</button>`);
    actions.push(`<button type="button" class="btn" data-action="backup-anyway" data-key="${esc(dev.key)}">Sauvegarder quand même</button>`);
    actions.push(`<button type="button" class="btn" data-action="eject" data-key="${esc(dev.key)}">Éjecter</button>`);
  }

  const html = `
    <div class="card-head"><span class="card-title">${heading}</span><span class="muted small">${meta}</span></div>
    ${body}
    ${actions.length ? `<div class="actions">${actions.join("")}</div>` : ""}`;
  return { sig: `${tone}|${html}`, tone, html, live };
}

function applyLive(el, live) {
  for (const [name, value] of Object.entries(live)) {
    const target = el.querySelector(`[data-live="${name}"]`);
    if (!target) continue;
    if (name === "bar") target.value = value;
    else if (target.textContent !== value) target.textContent = value;
  }
}

function renderCards() {
  const container = $("#cards");
  const jobs = jobsByDevice();
  const devices = state.devices.filter((d) => d.role !== "destination");
  if (!devices.length) {
    const html = `<p class="empty">Insérez une carte (GoPro, drone, appareil photo) dans le lecteur USB.</p>`;
    if (container.dataset.empty !== "1") { container.innerHTML = html; container.dataset.empty = "1"; }
    return;
  }
  if (container.dataset.empty === "1") { container.innerHTML = ""; delete container.dataset.empty; }
  const seen = new Set();
  let previous = null;
  for (const dev of devices) {
    seen.add(dev.key);
    const view = deviceView(dev, jobs.get(dev.key));
    let el = container.querySelector(`[data-key-card="${CSS.escape(dev.key)}"]`);
    if (!el) {
      el = document.createElement("article");
      el.dataset.keyCard = dev.key;
    }
    if (el.dataset.sig !== view.sig) {
      el.className = `card ${view.tone}`;
      el.innerHTML = view.html;
      el.dataset.sig = view.sig;
    }
    applyLive(el, view.live);
    const expected = previous ? previous.nextSibling : container.firstChild;
    if (expected !== el) container.insertBefore(el, expected);
    previous = el;
  }
  for (const el of [...container.children]) {
    if (!seen.has(el.dataset.keyCard)) el.remove();
  }
}

function renderSettings() {
  if (settingsDirty) return;
  const form = $("#settings-form");
  if (form.contains(document.activeElement)) return;
  for (const [name, value] of Object.entries(state.settings)) {
    const input = form.elements[name];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
  }
}

function renderSystem() {
  const h = state.health || {};
  const p = h.power || {};
  const clock = h.clock_synchronized === true ? "synchronisée"
    : h.clock_synchronized === false ? "non synchronisée (pas de réseau ; le Pi 4 n'a pas d'horloge à pile)"
    : "inconnue";
  const power = !p.available ? "non mesurée"
    : p.under_voltage_now ? "SOUS-TENSION en ce moment"
    : p.under_voltage_since_boot ? "sous-tension depuis le démarrage"
    : "correcte";
  const html = `<dl class="kv">
      <dt>Version</dt><dd>${esc(state.version)}</dd>
      <dt>Alimentation</dt><dd>${esc(power)}</dd>
      <dt>Heure du Pi</dt><dd>${esc(clock)}</dd>
    </dl>`;
  const el = $("#system");
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
}

function renderNotices() {
  const notices = state.notices || [];
  if (lastNoticeId === null) {
    lastNoticeId = notices.length ? notices[notices.length - 1].id : 0;
    return;
  }
  for (const n of notices) {
    if (n.id > lastNoticeId) {
      toast(n.text, n.level === "error" ? "error" : n.level === "success" ? "success" : "info");
      lastNoticeId = n.id;
    }
  }
}

// ----------------------------------------------------------------- history

async function loadHistory() {
  const el = $("#history");
  try {
    const records = await api("GET", "/api/history");
    if (!records.length) {
      el.innerHTML = `<p class="empty">Aucune sauvegarde pour l'instant.</p>`;
      return;
    }
    el.innerHTML = records.map((r) => `
      <article class="card ${jobTone(r)}">
        <div class="card-head">
          <span class="card-title">${esc(r.device_name)} → <code>${esc(r.source_name)}</code></span>
          <span class="muted small">${esc(fmtDate(r.finished_at || r.created_at))}</span>
        </div>
        <div class="state-line ${jobTone(r)}">${esc(JOB_LABELS[r.state] || r.state)}${r.project ? ` · ${esc(r.project)}` : ""}</div>
        ${r.message ? `<div class="small">${esc(r.message)}</div>` : ""}
        ${r.error ? `<div class="small state-line err">${esc(r.error)}</div>` : ""}
        <div class="actions"><button type="button" class="btn" data-action="details" data-job="${esc(r.id)}">Détails</button></div>
      </article>`).join("");
  } catch (err) {
    el.innerHTML = `<p class="state-line err">${esc(err.message)}</p>`;
  }
}

$("#history-panel").addEventListener("toggle", (e) => {
  if (e.target.open) loadHistory();
});

// ----------------------------------------------------------------- dialogs

function openDialog(id) {
  const dialog = document.getElementById(id);
  dialog.returnValue = "";
  dialog.showModal();
  return dialog;
}

function onClose(dialog, handler) {
  dialog.addEventListener("close", () => {
    const value = dialog.returnValue;
    dialog.returnValue = "";
    handler(value);
  }, { once: true });
}

function confirmAction(text, okLabel) {
  return new Promise((resolve) => {
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = okLabel;
    const dialog = openDialog("confirm-dialog");
    onClose(dialog, (value) => resolve(value === "ok"));
  });
}

function choiceList(container, name, values, current, labelFor) {
  container.innerHTML = values.map((v, i) => `
    <label class="choice"><input type="radio" name="${name}" value="${esc(v)}" ${v === current ? "checked" : ""} id="${name}-${i}"> ${esc(labelFor ? labelFor(v) : v)}</label>`).join("");
}

async function chooseSource(jobId) {
  const job = state.jobs.find((j) => j.id === jobId);
  if (!job) return;
  if (["countdown", "queued", "waiting"].includes(job.state)) {
    try { await api("POST", `/api/jobs/${encodeURIComponent(jobId)}/hold`); } catch { /* shown on refresh */ }
  }
  let sources = [];
  try { sources = (await api("GET", "/api/destination/projects")).sources; } catch { sources = []; }
  const values = [...new Set([job.source_name, ...sources])];
  const form = $("#source-form");
  form.reset();
  choiceList($("#source-choices"), "source", values, job.source_name);
  const dialog = openDialog("source-dialog");
  onClose(dialog, async (value) => {
    if (value !== "start") { refresh(); return; }
    const typed = form.elements.new_source.value.trim();
    const picked = form.querySelector('input[name="source"]:checked');
    const name = typed || (picked && picked.value) || job.source_name;
    try {
      await api("POST", `/api/jobs/${encodeURIComponent(jobId)}/source`, { name });
      await act("POST", `/api/jobs/${encodeURIComponent(jobId)}/start`);
    } catch (err) {
      toast(err.message, "error");
      refresh();
    }
  });
}

async function chooseProject() {
  let info;
  try { info = await api("GET", "/api/destination/projects"); } catch (err) { toast(err.message, "error"); return; }
  const form = $("#project-form");
  form.reset();
  choiceList($("#project-choices"), "project", info.projects, info.current, (v) => (v === "" ? "(racine du SSD)" : v));
  if (!info.projects.length) $("#project-choices").innerHTML = `<p class="muted">Aucun projet sur ce SSD pour l'instant.</p>`;
  form.elements.new_project.placeholder = suggestProject();
  const dialog = openDialog("project-dialog");
  onClose(dialog, async (value) => {
    if (value !== "save") return;
    const typed = form.elements.new_project.value.trim();
    const picked = form.querySelector('input[name="project"]:checked');
    const project = typed || (picked ? picked.value : null);
    if (project === null) { toast("Choisissez ou saisissez un projet.", "error"); return; }
    try { await act("POST", "/api/destination/project", { project }); } catch { /* toasted */ }
  });
}

function suggestProject() {
  const d = new Date();
  return `Voyage_${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function initDestination(key) {
  const form = $("#init-form");
  form.reset();
  form.elements.project.value = suggestProject();
  const dialog = openDialog("init-dialog");
  onClose(dialog, async (value) => {
    if (value !== "init") return;
    try {
      await act("POST", `/api/devices/${encodeURIComponent(key)}/init-destination`, { project: form.elements.project.value.trim() });
    } catch { /* toasted */ }
  });
}

async function showDetails(jobId) {
  let job;
  try { job = await api("GET", `/api/jobs/${encodeURIComponent(jobId)}`); } catch (err) { toast(err.message, "error"); return; }
  const s = job.summary || {};
  const rows = [
    ["Carte", job.device_name],
    ["Source", job.source_name],
    ["Projet", job.project === "" ? "(racine du SSD)" : job.project],
    ["SSD", job.destination_name],
    ["État", JOB_LABELS[job.state] || job.state],
    ["Début", fmtDate(job.started_at)],
    ["Durée", job.duration_s != null ? fmtDuration(job.duration_s) : ""],
    ["Fichiers", s.files != null ? `${s.files} (copiés ${s.copied ?? "–"}, déjà présents ${s.skipped ?? "–"}, conflits ${s.conflicts ?? 0}, échecs ${s.failed ?? 0})` : ""],
    ["Taille de la carte", s.bytes_card != null ? fmtBytes(s.bytes_card) : ""],
    ["Vérification", s.verified === false ? "désactivée" : s.verified ? "relecture complète sur le SSD" : ""],
  ].filter(([, v]) => v);
  const problems = job.problems || [];
  $("#detail-body").innerHTML = `
    <dl class="kv">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    ${job.message ? `<p>${esc(job.message)}</p>` : ""}
    ${job.error ? `<p class="state-line err">${esc(job.error)}</p>` : ""}
    ${problems.length ? `<h3>Fichiers à surveiller (${problems.length})</h3><ul class="problems">${problems.slice(0, 200).map((p) => `<li><strong>${esc(p.type)}</strong> · <code>${esc(p.file)}</code><br>${esc(p.detail || "")}</li>`).join("")}</ul>` : ""}`;
  openDialog("detail-dialog");
}

// ----------------------------------------------------------------- actions

document.addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-action]");
  if (!btn) return;
  const { action, key, job } = btn.dataset;
  const k = key ? encodeURIComponent(key) : "";
  const j = job ? encodeURIComponent(job) : "";
  btn.disabled = true;
  try {
    switch (action) {
      case "start": await act("POST", `/api/jobs/${j}/start`); break;
      case "cancel":
        if (await confirmAction("Annuler cette sauvegarde ? Les fichiers déjà vérifiés restent sur le SSD.", "Annuler la sauvegarde")) {
          await act("POST", `/api/jobs/${j}/cancel`);
        }
        break;
      case "source": await chooseSource(job); break;
      case "backup": await act("POST", `/api/devices/${k}/backup`, {}); break;
      case "backup-anyway":
        if (await confirmAction("Ce disque n'a pas de dossier DCIM. Tout son contenu sera copié dans le projet. Continuer ?", "Sauvegarder")) {
          await act("POST", `/api/devices/${k}/backup`, {});
        }
        break;
      case "eject": await act("POST", `/api/devices/${k}/eject`); break;
      case "mount": await act("POST", `/api/devices/${k}/mount`); break;
      case "init": initDestination(key); break;
      case "choose-project": await chooseProject(); break;
      case "details": await showDetails(job); break;
      case "power": {
        const off = btn.dataset.power === "poweroff";
        const ok = await confirmAction(
          off ? "Éteindre BackupBox ? Attendez que la DEL verte cesse de clignoter avant de couper le courant." : "Redémarrer BackupBox ?",
          off ? "Éteindre" : "Redémarrer",
        );
        if (ok) {
          await api("POST", `/api/system/${btn.dataset.power}`);
          toast(off ? "Arrêt en cours…" : "Redémarrage en cours…");
        }
        break;
      }
      default: break;
    }
  } catch {
    /* already reported */
  } finally {
    btn.disabled = false;
  }
});

document.addEventListener("change", async (event) => {
  const select = event.target.closest('select[data-action="select-dest"]');
  if (select) {
    try { await act("POST", `/api/devices/${encodeURIComponent(select.value)}/select`); } catch { /* toasted */ }
  }
});

$("#settings-form").addEventListener("change", async (event) => {
  const input = event.target;
  if (!input.name) return;
  if (!input.checkValidity()) { input.reportValidity(); return; }
  const value = input.type === "checkbox" ? input.checked : Number(input.value);
  settingsDirty = true;
  try {
    await api("PUT", "/api/settings", { [input.name]: value });
    toast("Réglage enregistré.", "success");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    settingsDirty = false;
    refresh();
  }
});

$("#settings-form").addEventListener("submit", (e) => e.preventDefault());

refresh().then(schedule);

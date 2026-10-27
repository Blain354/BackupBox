"use strict";

// BackupBox web UI: polls /api/state every second and renders it.
// Blocks are rebuilt only when their structure changes; live numbers
// (progress, speed, countdown) are patched in place so buttons stay tappable.

const POLL_MS = 1000;
const HIDDEN_POLL_MS = 5000;
const ACTIVE = ["waiting", "held", "countdown", "queued", "running"];
const PENDING = ["waiting", "held", "countdown"];

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

function plural(n, one, many) {
  return `${n} ${n > 1 ? many : one}`;
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
  setTimeout(() => el.remove(), level === "error" ? 8000 : 4500);
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
  done: "Sauvegardé et vérifié",
  partial: "Sauvegarde incomplète",
  failed: "Échec",
  cancelled: "Annulée",
};

const CONNECTIONS = {
  direct: "branché par câble",
  mtp: "branché par câble (MTP)",
  reader: "carte dans le lecteur",
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
  for (const job of state.jobs) if (job.kind === "card") map.set(job.device_key, job);
  return map;
}

function deviceByKey(key) {
  return state.devices.find((d) => d.key === key) || null;
}

function externalSsd() {
  return state.devices.find((d) => d.key === state.external_destination) || null;
}

function deviceIcon(dev) {
  const brand = dev && dev.card && dev.card.brand;
  if (brand === "DJI") return "🚁";
  if (brand === "GoPro" || brand === "Insta360") return "🎥";
  if (dev && dev.card && dev.card.is_camera) return "📷";
  return "💾";
}

function targetText(job) {
  if (job.target === "buffer") {
    const b = state.buffer;
    const room = b && b.usable_bytes != null ? ` · ${fmtBytes(b.usable_bytes)} disponibles` : "";
    return `<strong>Tampon du Pi</strong> (pas de SSD)${room}`;
  }
  if (job.target === "ssd") {
    return `<strong>SSD « ${esc(job.destination_name)} »</strong>${job.project ? ` · ${esc(job.project)}` : ""}`;
  }
  return "";
}

function plannedBytes(inv, preset) {
  if (!inv) return null;
  if (preset === "videos_only") return inv.video.bytes;
  if (preset === "no_previews") return inv.total.bytes - inv.preview.bytes;
  return inv.total.bytes;
}

function inventoryText(inv) {
  if (!inv) return "Analyse du contenu…";
  const parts = [];
  if (inv.video.files) parts.push(plural(inv.video.files, "vidéo", "vidéos"));
  if (inv.photo.files) parts.push(plural(inv.photo.files, "photo", "photos"));
  const side = inv.preview.files + inv.other.files;
  if (side) parts.push(`${plural(side, "fichier annexe", "fichiers annexes")} (${fmtBytes(inv.preview.bytes + inv.other.bytes)})`);
  if (!parts.length) return "Aucun fichier.";
  return `${parts.join(" · ")} — ${fmtBytes(inv.total.bytes)} au total`;
}

function startLabel(preset) {
  if (preset === "videos_only") return "Transférer les vidéos";
  if (preset === "no_previews") return "Transférer (sans aperçus)";
  return "Tout transférer";
}

// ------------------------------------------------------------------ render

function render() {
  renderStatus();
  renderBanners();
  renderQuick();
  renderDestination();
  renderBuffer();
  renderCards();
  renderSettings();
  renderSystem();
  renderNotices();
}

function renderStatus() {
  const chip = $("#status-chip");
  const writing = state.jobs.some((j) => ["running", "queued"].includes(j.state));
  const pending = state.jobs.some((j) => PENDING.includes(j.state));
  const failed = state.jobs.some((j) => ["failed", "partial"].includes(j.state));
  let text = "Prête";
  let tone = "ok";
  if (writing) { text = "Écriture…"; tone = "busy"; }
  else if (pending) { text = "Action possible"; tone = "warn"; }
  else if (failed) { text = "Attention"; tone = "err"; }
  else if (!externalSsd() && !state.buffer) { text = "SSD absent"; tone = "warn"; }
  chip.textContent = text;
  chip.className = `chip ${tone}`;
}

function renderBanners() {
  const out = [];
  if (connectionLost) out.push(`<div class="banner err">Connexion à BackupBox perdue. Vérifiez le Wi-Fi « BackupBox ».</div>`);
  if (state) {
    if (state.jobs.some((j) => j.state === "running")) {
      out.push(`<div class="banner warn">Écriture en cours : ne débranchez rien et n'éteignez pas BackupBox.</div>`);
    }
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

// Keyed list rendering: rebuild an item only when its signature changes.
function renderKeyed(container, items, attr) {
  const seen = new Set();
  let previous = null;
  for (const item of items) {
    seen.add(item.key);
    let el = container.querySelector(`[data-${attr}="${CSS.escape(item.key)}"]`);
    if (!el) {
      el = document.createElement("article");
      el.dataset[attr.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = item.key;
    }
    if (el.dataset.sig !== item.sig) {
      el.className = item.className;
      el.innerHTML = item.html;
      el.dataset.sig = item.sig;
    }
    applyLive(el, item.live || {});
    const expected = previous ? previous.nextSibling : container.firstChild;
    if (expected !== el) container.insertBefore(el, expected);
    previous = el;
  }
  for (const el of [...container.children]) {
    if (!seen.has(el.getAttribute(`data-${attr}`))) el.remove();
  }
}

function applyLive(el, live) {
  for (const [name, value] of Object.entries(live)) {
    const target = el.querySelector(`[data-live="${name}"]`);
    if (!target) continue;
    if (name === "bar") target.value = value;
    else if (target.textContent !== value) target.textContent = value;
  }
}

function progressBlock(job) {
  const p = job.progress;
  const html = `
    <progress max="100" value="0" data-live="bar" aria-label="Progression"></progress>
    <div class="stats"><span data-live="percent"></span><span data-live="files"></span><span data-live="speed"></span><span data-live="eta"></span></div>
    <div class="current" data-live="current"></div>`;
  const live = {
    bar: p.percent,
    percent: `${p.percent.toLocaleString("fr-CA")} %`,
    files: `${p.files_done} / ${p.files_total || "?"} fichiers`,
    speed: p.speed_bps ? `${fmtBytes(p.speed_bps)}/s` : "",
    eta: p.eta_s != null ? `reste ≈ ${fmtDuration(p.eta_s)}` : "",
    current: p.current_file ? `${p.current_step || p.phase} : ${p.current_file}` : p.phase,
  };
  return { html, live };
}

function quickItem(job) {
  const live = {};
  const jid = esc(job.id);
  if (job.kind === "drain") {
    const head = `<div class="quick-head"><span class="device-icon" aria-hidden="true">📦</span><div>
        <div class="quick-title">Tampon du Pi → SSD « ${esc(job.destination_name || "")} »</div>
        <div class="muted small"><code>${esc(job.source_name)}</code> · ${esc(job.project || "")} — déplacé puis effacé du Pi une fois vérifié</div></div></div>`;
    let body = "";
    if (job.state === "running") {
      const block = progressBlock(job);
      body = block.html;
      Object.assign(live, block.live);
    } else {
      body = `<div class="state-line">En file d'attente</div>`;
    }
    const html = `${head}${body}<div class="actions"><button type="button" class="btn danger" data-action="cancel" data-job="${jid}">Annuler</button></div>`;
    return { key: job.id, sig: html, className: "card quick busy", html, live };
  }

  const dev = deviceByKey(job.device_key);
  const conn = dev && dev.card ? CONNECTIONS[dev.card.connection] || "" : "";
  const head = `<div class="quick-head"><span class="device-icon" aria-hidden="true">${deviceIcon(dev)}</span><div>
      <div class="quick-title">${esc(job.device_name)}</div>
      <div class="muted small">${esc(conn)}${dev && dev.read_only ? " · lecture seule" : ""}</div></div></div>`;
  const inv = dev ? dev.inventory : null;
  let body = `<div class="inventory">${esc(inventoryText(inv))}</div>`;
  let actions = [];
  let tone = "busy";

  if (job.state === "running") {
    const block = progressBlock(job);
    body += `<div>→ ${targetText(job)} · dossier <code>${esc(job.source_name)}</code></div>${block.html}`;
    Object.assign(live, block.live);
    actions.push(`<button type="button" class="btn danger" data-action="cancel" data-job="${jid}">Annuler</button>`);
  } else if (job.state === "queued") {
    body += `<div>→ ${targetText(job)} · dossier <code>${esc(job.source_name)}</code></div><div class="state-line">Démarre dans un instant…</div>`;
    actions.push(`<button type="button" class="btn danger" data-action="cancel" data-job="${jid}">Annuler</button>`);
  } else if (job.state === "waiting") {
    tone = "warn";
    body += `<div class="state-line warn">${esc(job.wait_reason || "En attente")}</div>`;
    actions.push(`<button type="button" class="btn" data-action="source" data-job="${jid}">Nommer l'appareil</button>`);
    actions.push(`<button type="button" class="btn danger" data-action="cancel" data-job="${jid}">Annuler</button>`);
  } else {
    tone = "warn";
    const planned = plannedBytes(inv, job.preset);
    const presets = Object.entries(state.presets || {}).map(([value, label]) => `
        <label class="seg"><input type="radio" name="preset-${jid}" value="${esc(value)}" data-job="${jid}" ${value === job.preset ? "checked" : ""}> ${esc(label)}</label>`).join("");
    body += `
      <div>→ ${targetText(job)}</div>
      <div class="small">Dossier : <code>${esc(job.source_name)}</code>${job.known_card ? " (appareil connu)" : " · <strong>nouvel appareil</strong>"}</div>
      <fieldset class="segmented"><legend class="small muted">Fichiers à copier</legend>${presets}</fieldset>
      <button type="button" class="btn primary big" data-action="start" data-job="${jid}">${esc(startLabel(job.preset))}${planned != null ? ` · ${esc(fmtBytes(planned))}` : ""}</button>
      ${job.state === "countdown" ? `<div class="muted small center">Démarrage automatique dans <strong data-live="countdown"></strong></div>` : `<div class="muted small center">En pause : rien ne démarre sans vous.</div>`}`;
    live.countdown = job.countdown_s != null ? `${job.countdown_s} s` : "";
    actions.push(`<button type="button" class="btn" data-action="source" data-job="${jid}">Changer le nom</button>`);
    actions.push(`<button type="button" class="btn danger" data-action="cancel" data-job="${jid}">Annuler</button>`);
  }
  const html = `${head}${body}<div class="actions">${actions.join("")}</div>`;
  return { key: job.id, sig: `${tone}|${html}`, className: `card quick ${tone}`, html, live };
}

function renderQuick() {
  const order = { running: 0, queued: 1, countdown: 2, held: 3, waiting: 4 };
  const jobs = state.jobs
    .filter((j) => ACTIVE.includes(j.state))
    .sort((a, b) => (order[a.state] - order[b.state]) || a.created_at.localeCompare(b.created_at));
  const panel = $("#quick-panel");
  panel.hidden = jobs.length === 0;
  renderKeyed($("#quick"), jobs.map(quickItem), "key-quick");
}

function renderDestination() {
  const el = $("#destination");
  const dests = state.devices.filter((d) => d.role === "destination" && d.status === "ready");
  const dest = externalSsd();
  let html;
  if (!dest) {
    html = state.buffer
      ? `<p class="empty">Pas de SSD branché : les appareils vont dans le tampon du Pi. Branchez le SSD pour tout y transférer.</p>`
      : `<p class="empty">Branchez le SSD de destination. Un disque jamais utilisé apparaît plus bas : « Utiliser comme SSD de destination ».</p>`;
  } else {
    const project = dest.project;
    const projectText = project === null || project === undefined
      ? `<span class="state-line warn">Aucun projet choisi</span>`
      : `<strong>${esc(project === "" ? "(racine du SSD)" : project)}</strong>`;
    const used = dest.total_bytes ? dest.total_bytes - dest.free_bytes : 0;
    html = `
      <div class="card ${project == null ? "warn" : "ok"}">
        <div class="card-head">
          <span class="card-title">💽 SSD « ${esc(dest.name)} »</span>
          <span class="muted small">${esc(dest.fstype || "")}</span>
        </div>
        <div>Projet (voyage) : ${projectText}</div>
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

function renderBuffer() {
  const el = $("#buffer");
  const b = state.buffer;
  let html = "";
  if (b) {
    const used = b.total_bytes != null && b.free_bytes != null ? b.total_bytes - b.free_bytes : 0;
    const draining = state.jobs.some((j) => j.kind === "drain" && ACTIVE.includes(j.state));
    const canDrain = b.pending_files > 0 && externalSsd() && !draining;
    const list = b.pending.length
      ? `<ul class="pending">${b.pending.map((p) => `<li><code>${esc(p.source)}</code> · ${esc(p.project)} — ${plural(p.files, "fichier", "fichiers")}, ${fmtBytes(p.bytes)}</li>`).join("")}</ul>`
      : `<div class="muted small">Rien en attente du SSD.</div>`;
    html = `
      <div class="card ${b.pending_files ? "warn" : "ok"}">
        <div class="card-head">
          <span class="card-title">📦 Tampon du Pi</span>
          <span class="muted small">${b.pending_files ? `${fmtBytes(b.pending_bytes)} en attente du SSD` : "rien en attente"}</span>
        </div>
        <meter min="0" max="${b.total_bytes || 1}" value="${used}" high="${(b.total_bytes || 1) * 0.85}" aria-label="Espace utilisé du tampon"></meter>
        <div class="muted small">${fmtBytes(b.usable_bytes)} utilisables (réserve système ${fmtBytes(b.reserve_bytes)})</div>
        ${list}
        ${canDrain ? `<div class="actions"><button type="button" class="btn primary" data-action="drain">Vider vers le SSD</button></div>` : ""}
      </div>`;
  }
  if (el.dataset.html !== html) {
    el.innerHTML = html;
    el.dataset.html = html;
  }
}

function deviceView(dev, job) {
  const brand = dev.card && dev.card.brand;
  const model = dev.card && dev.card.model;
  const title = model ? (brand && !model.toLowerCase().startsWith(brand.toLowerCase()) ? `${brand} ${model}` : model)
    : (brand && brand.toLowerCase() !== String(dev.name).toLowerCase() ? `${brand} · ${dev.name}` : dev.name);
  const conn = dev.card ? CONNECTIONS[dev.card.connection] || "" : "";
  const meta = [conn, dev.kind === "block" ? fmtBytes(dev.size) : "", dev.read_only ? "lecture seule" : ""].filter(Boolean).join(" · ");
  let tone = "";
  let body = "";
  const actions = [];

  if (dev.status === "mounting") {
    body = `<div class="muted">Connexion…</div>`;
  } else if (dev.status === "ejected") {
    tone = job && job.state === "done" ? "ok" : "";
    const where = job && job.target === "buffer" ? "tampon du Pi" : `SSD « ${job ? job.destination_name || "" : ""} »`;
    body = job && job.state === "done"
      ? `<div class="state-line ok">✓ Sauvegardé et vérifié → ${esc(where)}, <code>${esc(job.source_name)}</code></div>
         <div>Vous pouvez le débrancher${job.target === "buffer" ? ". Gardez une copie sur l'appareil tant que le tampon n'est pas vidé vers le SSD." : " et formater la carte dans l'appareil."}</div>
         <div class="muted small">${esc(job.message || "")}</div>`
      : `<div class="state-line">Éjecté : vous pouvez le débrancher.</div>`;
    actions.push(`<button type="button" class="btn" data-action="mount" data-key="${esc(dev.key)}">Reconnecter</button>`);
    if (job) actions.push(`<button type="button" class="btn" data-action="details" data-job="${esc(job.id)}">Détails</button>`);
  } else if (dev.status === "error") {
    tone = "err";
    body = `<div class="state-line err">${esc(dev.error || "Erreur")}</div>`;
    actions.push(`<button type="button" class="btn" data-action="mount" data-key="${esc(dev.key)}">Réessayer</button>`);
  } else if (job && ACTIVE.includes(job.state)) {
    tone = "busy";
    body = `<div class="muted">${esc(JOB_LABELS[job.state])} — voir « À faire maintenant ».</div>`;
  } else if (job) {
    tone = jobTone(job);
    const where = job.target === "buffer" ? "tampon du Pi" : `SSD « ${job.destination_name || ""} »`;
    body = `<div class="state-line ${tone}">${job.state === "done" ? "✓ " : ""}${esc(JOB_LABELS[job.state] || job.state)} → ${esc(where)}, <code>${esc(job.source_name)}</code></div>`;
    if (job.message) body += `<div>${esc(job.message)}</div>`;
    if (job.error) body += `<div class="state-line err">${esc(job.error)}</div>`;
    if (job.state !== "done") actions.push(`<button type="button" class="btn primary" data-action="backup" data-key="${esc(dev.key)}">Relancer</button>`);
    actions.push(`<button type="button" class="btn" data-action="details" data-job="${esc(job.id)}">Détails</button>`);
    actions.push(`<button type="button" class="btn" data-action="eject" data-key="${esc(dev.key)}">Éjecter</button>`);
  } else if (dev.role === "source") {
    body = `<div>${esc(inventoryText(dev.inventory))}</div><div class="muted small">Sauvegarde automatique désactivée ou annulée.</div>`;
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
    <div class="card-head"><span class="card-title">${deviceIcon(dev)} ${esc(title)}</span><span class="muted small">${esc(meta)}</span></div>
    ${body}
    ${actions.length ? `<div class="actions">${actions.join("")}</div>` : ""}`;
  return { key: dev.key, sig: `${tone}|${html}`, className: `card ${tone}`, html, live: {} };
}

function renderCards() {
  const container = $("#cards");
  const jobs = jobsByDevice();
  const devices = state.devices.filter((d) => d.role !== "destination");
  if (!devices.length) {
    const html = `<p class="empty">Branchez un drone, une GoPro ou un appareil photo avec son câble USB, ou insérez une carte dans le lecteur.</p>`;
    if (container.dataset.empty !== "1") { container.innerHTML = html; container.dataset.empty = "1"; }
    return;
  }
  if (container.dataset.empty === "1") { container.innerHTML = ""; delete container.dataset.empty; }
  renderKeyed(container, devices.map((d) => deviceView(d, jobs.get(d.key))), "key-card");
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
          <span class="card-title">${r.kind === "drain" ? "📦 Tampon → SSD" : esc(r.device_name)} → <code>${esc(r.source_name)}</code></span>
          <span class="muted small">${esc(fmtDate(r.finished_at || r.created_at))}</span>
        </div>
        <div class="state-line ${jobTone(r)}">${esc(JOB_LABELS[r.state] || r.state)}${r.project ? ` · ${esc(r.project)}` : ""}${r.target === "buffer" ? " · tampon du Pi" : ""}</div>
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
  if (["countdown", "queued"].includes(job.state)) {
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
      if (job.state !== "waiting") await act("POST", `/api/jobs/${encodeURIComponent(jobId)}/start`);
      else await refresh();
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
  const presetLabel = state && state.presets ? state.presets[s.preset || job.preset] : "";
  const rows = [
    ["Appareil", job.kind === "drain" ? "Tampon du Pi" : job.device_name],
    ["Dossier", job.source_name],
    ["Projet", job.project === "" ? "(racine du SSD)" : job.project],
    ["Destination", job.target === "buffer" ? "Tampon du Pi" : job.destination_name],
    ["État", JOB_LABELS[job.state] || job.state],
    ["Début", fmtDate(job.started_at)],
    ["Durée", job.duration_s != null ? fmtDuration(job.duration_s) : ""],
    ["Fichiers", s.files != null ? `${s.files} (copiés ${s.copied ?? "–"}, déjà présents ${s.skipped ?? "–"}, conflits ${s.conflicts ?? 0}, échecs ${s.failed ?? 0})` : ""],
    ["Préréglage", presetLabel || ""],
    ["Ignorés", s.excluded_files ? `${s.excluded_files} (${fmtBytes(s.excluded_bytes)})` : ""],
    ["Taille de la source", s.bytes_card != null ? fmtBytes(s.bytes_card) : ""],
    ["Vérification", s.verified === false ? "désactivée" : s.verified ? "relecture complète sur le disque" : ""],
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
        if (await confirmAction("Annuler ? Les fichiers déjà vérifiés restent copiés.", "Annuler la sauvegarde")) {
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
      case "drain": await act("POST", "/api/buffer/drain"); break;
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
    return;
  }
  const preset = event.target.closest('input[type="radio"][data-job]');
  if (preset) {
    try { await act("POST", `/api/jobs/${encodeURIComponent(preset.dataset.job)}/preset`, { preset: preset.value }); } catch { /* toasted */ }
  }
});

$("#settings-form").addEventListener("change", async (event) => {
  const input = event.target;
  if (!input.name) return;
  if (!input.checkValidity()) { input.reportValidity(); return; }
  const value = input.type === "checkbox" ? input.checked
    : input.type === "number" ? Number(input.value)
    : input.value.trim();
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

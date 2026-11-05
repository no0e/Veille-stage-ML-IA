"use strict";

const TIER_RANK = { S: 0, A: 1, B: 2, C: 3 };
const TIER_NAMES = { S: "Élite", A: "Très prestigieux", B: "Solide", C: "Correct" };

const state = { jobs: [], sources: [], types: {}, apps: [], stages: {}, excluded: [], openCompanies: new Set(),
  openJobs: new Set(), pollTimer: null };
const filters = loadFilters();

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function loadFilters() {
  const defaults = { q: "", cats: ["ML", "DL", "Stats"], zone: "selection", status: "a_voir", tier: "all", sort: "prestige", newOnly: false };
  try {
    const saved = JSON.parse(localStorage.getItem("veille-filtres") || "{}");
    return { ...defaults, ...saved };
  } catch {
    return defaults;
  }
}

function saveFilters() {
  try { localStorage.setItem("veille-filtres", JSON.stringify(filters)); } catch { /* stockage indisponible */ }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Erreur ${response.status}`);
  return data;
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  return d.toLocaleDateString("fr-FR", { day: "numeric", month: "short", year: "numeric" });
}

function ago(iso) {
  if (!iso) return "jamais";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 60) return `il y a ${minutes} min`;
  if (minutes < 60 * 24) return `il y a ${Math.round(minutes / 60)} h`;
  return `il y a ${Math.round(minutes / 1440)} j`;
}

// --- collecte -------------------------------------------------------------------------------

async function loadState() {
  const s = await api("/api/state");
  $("#lastRefresh").textContent = `· mise à jour ${ago(s.last_refresh)}`;
  renderProgress(s.progress);
  return s;
}

function renderProgress(p) {
  const btn = $("#refreshBtn");
  if (p.running) {
    btn.disabled = true;
    $("#progress").textContent = `Collecte ${p.done}/${p.total}`;
    if (!state.pollTimer) state.pollTimer = setInterval(pollProgress, 2000);
  } else {
    btn.disabled = false;
    if (p.finished_at) {
      const errors = p.errors.length ? ` · ${p.errors.length} source(s) en erreur` : "";
      $("#progress").textContent = `${p.new_offers} nouvelle(s) offre(s)${errors}`;
    }
  }
}

async function pollProgress() {
  const s = await loadState();
  if (!s.progress.running) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    await Promise.all([loadJobs(), loadSources()]);
  }
}

async function refresh(sourceIds) {
  const r = await api("/api/refresh", { method: "POST", body: sourceIds ? { source_ids: sourceIds } : {} });
  renderProgress(r.progress);
}

// --- offres, regroupées par entreprise ------------------------------------------------------

async function loadJobs() {
  state.jobs = await api("/api/jobs");
  renderJobs();
}

function matchesZone(j) {
  switch (filters.zone) {
    case "europe": return j.region !== "Monde";
    case "france": return j.countries.includes("FR");
    case "tout": return true;
    default: return j.visible;
  }
}

function matchesStatus(j) {
  switch (filters.status) {
    case "tous": return true;
    case "a_voir": return ["nouveau", "interesse", "secondaire"].includes(j.status);
    case "interesse": return j.status === "interesse" || j.status === "secondaire";
    default: return j.status === filters.status;
  }
}

function filteredJobs() {
  const q = filters.q.trim().toLowerCase();
  const maxTier = filters.tier === "all" ? 9 : TIER_RANK[filters.tier];
  return state.jobs.filter((j) =>
    matchesZone(j) && matchesStatus(j) && TIER_RANK[j.tier] <= maxTier &&
    j.categories.some((c) => filters.cats.includes(c)) &&
    (!filters.newOnly || j.is_new) &&
    (!q || [j.company, j.title, j.location].join(" ").toLowerCase().includes(q)));
}

function groupByCompany(jobs) {
  const groups = new Map();
  for (const j of jobs) {
    if (!groups.has(j.company_key)) groups.set(j.company_key, { key: j.company_key, name: j.company, jobs: [] });
    groups.get(j.company_key).jobs.push(j);
  }
  const list = [...groups.values()];
  for (const g of list) {
    g.jobs.sort((a, b) => (b.posted_at || b.first_seen).localeCompare(a.posted_at || a.first_seen));
    g.tier = g.jobs.reduce((best, j) => (TIER_RANK[j.tier] < TIER_RANK[best] ? j.tier : best), "C");
    g.latest = g.jobs[0].posted_at || g.jobs[0].first_seen;
    g.hasNew = g.jobs.some((j) => j.is_new);
  }
  const byName = (a, b) => a.name.localeCompare(b.name, "fr", { sensitivity: "base" });
  const sorters = {
    prestige: (a, b) => TIER_RANK[a.tier] - TIER_RANK[b.tier] || b.jobs.length - a.jobs.length || byName(a, b),
    count: (a, b) => b.jobs.length - a.jobs.length || byName(a, b),
    recent: (a, b) => b.latest.localeCompare(a.latest) || byName(a, b),
    name: byName,
  };
  return list.sort(sorters[filters.sort] || sorters.prestige);
}

function jobRow(j) {
  const facts = [
    j.location ? `${j.flags} ${esc(j.location)}` : "",
    j.posted_at ? `publiée le ${fmtDate(j.posted_at)}` : `vue le ${fmtDate(j.first_seen)}`,
    j.start_date ? `début ${esc(j.start_date)}` : "",
    j.deadline ? `<strong>date limite ${fmtDate(j.deadline)}</strong>` : "",
    j.stipend ? esc(j.stipend) : "",
  ].filter(Boolean).join(" · ");
  const also = j.also.length
    ? `<div class="meta">aussi sur ${j.also.map((a) => `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.source)}</a>`).join(", ")}</div>` : "";
  const warn = j.warnings.length ? `<div class="warn-line">${j.warnings.map(esc).join(" · ")}</div>` : "";
  const open = state.openJobs.has(j.id);
  const st = (s, label, title) =>
    `<button class="btn small ${j.status === s ? "on" : ""}" data-status="${s}" title="${title}">${label}</button>`;
  return `<div class="job" data-id="${esc(j.id)}">
    <div class="job-main">
      <a class="job-title" href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
      ${j.is_new ? '<span class="pill new">nouveau</span>' : ""}
      <div class="meta">${facts}</div>${also}${warn}
      ${open ? `<div class="desc" id="desc-${esc(j.id)}">Chargement…</div>` : ""}
    </div>
    <div class="job-actions">
      <button class="btn small" data-action="text">${open ? "Masquer le texte" : "Texte"}</button>
      ${st("interesse", "★", "Intéressé")}${st("postule", "✓ Postulé", "J'ai postulé : ajoute la candidature au suivi")}${st("ecarte", "✕", "Écarter")}
    </div>
  </div>`;
}

function renderJobs() {
  const jobs = filteredJobs();
  const groups = groupByCompany(jobs);
  $("#countOffres").textContent = `(${jobs.length})`;
  $("#count").textContent = `${jobs.length} offre(s) dans ${groups.length} entreprise(s)`;
  $("#companies").innerHTML = groups.map((g) => {
    const open = state.openCompanies.has(g.key) || filters.q.trim() !== "";
    return `<div class="company ${open ? "open" : ""}" data-key="${esc(g.key)}" data-name="${esc(g.name)}">
      <div class="company-head" data-action="toggle">
        <span class="caret">${open ? "▾" : "▸"}</span>
        <strong>${esc(g.name)}</strong>
        <span class="tier tier-${g.tier}">${TIER_NAMES[g.tier]}</span>
        <span class="meta">${g.jobs.length} offre(s)${g.hasNew ? " · nouvelles" : ""}</span>
        <span class="spacer"></span>
        <button class="btn small ghost" data-action="hide-company" title="Ne plus afficher cette entreprise">Masquer l'entreprise</button>
      </div>
      ${open ? `<div class="company-jobs">${g.jobs.map(jobRow).join("")}</div>` : ""}
    </div>`;
  }).join("") || '<p class="meta">Aucune offre avec ces filtres.</p>';
  for (const id of state.openJobs) loadDescription(id);
}

async function loadDescription(id) {
  const box = document.getElementById(`desc-${id}`);
  if (!box) return;
  try {
    const j = await api(`/api/job?id=${encodeURIComponent(id)}`);
    box.textContent = j.description || "Pas de texte enregistré : ouvre le lien.";
  } catch {
    box.textContent = "Texte indisponible.";
  }
}

async function onCompaniesClick(event) {
  const target = event.target.closest("[data-action], [data-status]");
  if (!target || target.tagName === "A") return;
  const company = target.closest(".company");
  const job = target.closest(".job");
  if (target.dataset.action === "toggle" && !event.target.closest("button")) {
    const key = company.dataset.key;
    state.openCompanies.has(key) ? state.openCompanies.delete(key) : state.openCompanies.add(key);
    renderJobs();
  } else if (target.dataset.action === "hide-company") {
    const name = company.dataset.name;
    if (!confirm(`Masquer toutes les offres de « ${name} » ? (réversible dans l'onglet Sources)`)) return;
    await api("/api/exclude-company", { method: "POST", body: { company: name } });
    await Promise.all([loadJobs(), loadSources()]);
  } else if (target.dataset.action === "text") {
    const id = job.dataset.id;
    state.openJobs.has(id) ? state.openJobs.delete(id) : state.openJobs.add(id);
    renderJobs();
  } else if (target.dataset.status) {
    const id = job.dataset.id;
    const current = state.jobs.find((j) => j.id === id);
    const status = current.status === target.dataset.status ? "nouveau" : target.dataset.status;
    if (current.status === "postule" && status !== "postule"
        && !confirm("Retirer cette offre du suivi des candidatures ? (les fichiers joints restent dans leur dossier)")) return;
    await api("/api/job/status", { method: "POST", body: { id, status } });
    current.status = status;
    renderJobs();
    if (status === "postule" || target.dataset.status === "postule") loadApplications();
  }
}

function bindFilters() {
  $("#q").value = filters.q;
  for (const id of ["zone", "status", "tier", "sort"]) $("#" + id).value = filters[id];
  $("#newOnly").checked = filters.newOnly;
  document.querySelectorAll("#catButtons [data-cat]").forEach((b) => b.classList.toggle("on", filters.cats.includes(b.dataset.cat)));
  $("#q").addEventListener("input", (e) => { filters.q = e.target.value; saveFilters(); renderJobs(); });
  for (const id of ["zone", "status", "tier", "sort"]) {
    $("#" + id).addEventListener("change", (e) => { filters[id] = e.target.value; saveFilters(); renderJobs(); });
  }
  $("#newOnly").addEventListener("change", (e) => { filters.newOnly = e.target.checked; saveFilters(); renderJobs(); });
  $("#catButtons").addEventListener("click", (e) => {
    const b = e.target.closest("[data-cat]");
    if (!b) return;
    const cat = b.dataset.cat;
    filters.cats = filters.cats.includes(cat) ? filters.cats.filter((c) => c !== cat) : [...filters.cats, cat];
    b.classList.toggle("on");
    saveFilters();
    renderJobs();
  });
}

// --- sources --------------------------------------------------------------------------------

async function loadSources() {
  const data = await api("/api/sources");
  state.sources = data.sources;
  state.types = data.types;
  state.excluded = data.excluded || [];
  renderSources();
  renderExcluded();
}

function tierSelect(source) {
  return `<select data-field="tier">${Object.keys(TIER_NAMES).map((t) =>
    `<option value="${t}" ${source.tier === t ? "selected" : ""}>${TIER_NAMES[t]}</option>`).join("")}</select>`;
}

function renderSources() {
  $("#countSources").textContent = `(${state.sources.length})`;
  $("#sourcesBody").innerHTML = state.sources.map((s) => {
    const isLink = s.type === "link";
    let status = '<span class="meta">jamais</span>';
    if (s.last_run) {
      status = s.last_status === "ok" || s.last_status === "vérifié"
        ? `<span class="status-ok">${isLink ? "ouverte" : "ok"}</span> <span class="meta">${ago(s.last_run)}</span>`
        : `<span class="status-err" title="${esc(s.last_status)}">erreur</span>`;
    }
    const action = isLink
      ? `<a class="btn small" href="${esc(s.open_url || s.params.url)}" target="_blank" rel="noopener" data-action="visit">Ouvrir ↗</a>`
      : `<button class="btn small" data-action="collect">Collecter</button>`;
    return `<tr data-id="${s.id}">
      <td><input type="checkbox" data-field="enabled" ${s.enabled ? "checked" : ""}></td>
      <td><strong>${esc(s.name)}</strong></td>
      <td>${esc(isLink ? "lien à ouvrir" : (state.types[s.type] || s.type))}</td>
      <td>${tierSelect(s)}</td>
      <td>${status}</td>
      <td>${isLink ? "—" : s.active_jobs}</td>
      <td><div class="row">${action}<button class="btn small" data-action="delete" title="Supprimer la source">✕</button></div></td>
    </tr>`;
  }).join("");
}

function renderExcluded() {
  $("#excludedList").innerHTML = state.excluded.length
    ? state.excluded.map((name) => `<span class="chip">${esc(name)} <button class="btn small" data-unexclude="${esc(name)}" title="Réafficher">✕</button></span>`).join("")
    : '<span class="meta">Aucune.</span>';
}

async function onExcludedClick(event) {
  const btn = event.target.closest("[data-unexclude]");
  if (!btn) return;
  await api("/api/exclude-company", { method: "POST", body: { company: btn.dataset.unexclude, undo: true } });
  await Promise.all([loadSources(), loadJobs(), loadState()]);
}

async function onSourcesChange(event) {
  const field = event.target.dataset.field;
  if (!field) return;
  const id = event.target.closest("tr").dataset.id;
  const value = event.target.type === "checkbox" ? event.target.checked : event.target.value;
  await api(`/api/sources/${id}`, { method: "PATCH", body: { [field]: value } });
  await Promise.all([loadSources(), loadJobs()]);
}

async function onSourcesClick(event) {
  const btn = event.target.closest("[data-action]");
  if (!btn) return;
  const id = Number(btn.closest("tr").dataset.id);
  const source = state.sources.find((s) => s.id === id);
  if (btn.dataset.action === "delete") {
    if (!confirm(`Supprimer la source « ${source.name} » et ses offres ?`)) return;
    await api(`/api/sources/${id}`, { method: "DELETE" });
    await Promise.all([loadSources(), loadJobs()]);
  } else if (btn.dataset.action === "collect") {
    await refresh([id]);
  } else if (btn.dataset.action === "visit") {
    await api(`/api/sources/${id}/visited`, { method: "POST" });
    await loadSources();
  }
}

async function detect() {
  const text = $("#detectInput").value.trim();
  const box = $("#detectResult");
  if (!text) return;
  box.innerHTML = '<div class="detect-result">Détection…</div>';
  try {
    const r = await api("/api/sources/detect", { method: "POST", body: { text } });
    if (!r.type) {
      box.innerHTML = `<div class="detect-result">${esc(r.message)}</div>`;
      return;
    }
    const guess = /^https?:/.test(text) ? (r.params.token || r.params.account || r.params.company || r.params.tenant || "") : text;
    box.innerHTML = `<div class="detect-result">
      <div><strong>${esc(state.types[r.type] || r.type)}</strong> · ${esc(r.message)}</div>
      <div class="fields">
        <input id="newName" placeholder="Nom affiché" value="${esc(guess.replace(/[-_.]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()))}">
        <select id="newTier">${Object.keys(TIER_NAMES).map((t) => `<option value="${t}" ${t === "B" ? "selected" : ""}>${TIER_NAMES[t]}</option>`).join("")}</select>
        <button class="btn primary" id="addSourceBtn">Ajouter la source</button>
      </div>
    </div>`;
    $("#addSourceBtn").addEventListener("click", async () => {
      try {
        await api("/api/sources", { method: "POST", body: { name: $("#newName").value, type: r.type, params: r.params, tier: $("#newTier").value } });
        box.innerHTML = '<div class="detect-result">Source ajoutée, collecte en cours.</div>';
        $("#detectInput").value = "";
        await loadSources();
        loadState();
      } catch (e) {
        alert(e.message);
      }
    });
  } catch (e) {
    box.innerHTML = `<div class="detect-result status-err">${esc(e.message)}</div>`;
  }
}

// --- candidatures ---------------------------------------------------------------------------

async function loadApplications() {
  const data = await api("/api/applications");
  state.apps = data.applications;
  state.stages = data.stages;
  renderApplications();
}

function fileCell(a, kind) {
  const name = a[`${kind}_file`];
  if (name) {
    return `<a href="/fichiers/${a.id}/${encodeURIComponent(name)}" target="_blank" rel="noopener" title="${esc(name)}">📄 ${esc(name.length > 22 ? name.slice(0, 20) + "…" : name)}</a>
      <button class="btn small ghost" data-remove="${kind}" title="Retirer">✕</button>`;
  }
  return `<label class="btn small upload">Joindre<input type="file" data-upload="${kind}" hidden></label>`;
}

function renderApplications() {
  $("#countSuivi").textContent = `(${state.apps.length})`;
  const options = (a) => Object.entries(state.stages).map(([k, v]) =>
    `<option value="${k}" ${a.stage === k ? "selected" : ""}>${esc(v)}</option>`).join("");
  $("#appsBody").innerHTML = state.apps.map((a) => `<tr data-id="${a.id}">
      <td><strong>${esc(a.company)}</strong><div>${a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>` : esc(a.title)}</div></td>
      <td><input type="date" data-field="applied_at" value="${esc(a.applied_at || "")}"></td>
      <td><select data-field="stage">${options(a)}</select></td>
      <td class="file-cell">${fileCell(a, "cv")}</td>
      <td class="file-cell">${fileCell(a, "lettre")}</td>
      <td><textarea data-field="notes" rows="1">${esc(a.notes || "")}</textarea></td>
      <td><div class="row">
        <button class="btn small ghost" data-action="folder" title="Ouvrir le dossier des fichiers">📁</button>
        <button class="btn small ghost" data-action="delete" title="Supprimer la candidature">✕</button>
      </div></td>
    </tr>`).join("") || '<tr><td colspan="7" class="meta">Aucune candidature. Clique « ✓ Postulé » sur une offre, ou ajoute-la ci-dessus.</td></tr>';
}

async function onAppChange(event) {
  const row = event.target.closest("tr");
  if (!row) return;
  const id = row.dataset.id;
  if (event.target.dataset.upload) {
    const file = event.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const response = await fetch(`/api/applications/${id}/file/${event.target.dataset.upload}`, { method: "POST", body: form });
    if (!response.ok) alert((await response.json().catch(() => ({}))).error || "Envoi impossible");
    await loadApplications();
    return;
  }
  const field = event.target.dataset.field;
  if (!field) return;
  await api(`/api/applications/${id}`, { method: "PATCH", body: { [field]: event.target.value } });
}

async function onAppClick(event) {
  const btn = event.target.closest("[data-action], [data-remove]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  if (btn.dataset.remove) {
    await api(`/api/applications/${id}/file/${btn.dataset.remove}`, { method: "DELETE" });
    await loadApplications();
  } else if (btn.dataset.action === "folder") {
    await api(`/api/applications/${id}/open-folder`, { method: "POST" });
  } else if (btn.dataset.action === "delete") {
    if (!confirm("Supprimer cette candidature du suivi ? (les fichiers restent dans le dossier)")) return;
    await api(`/api/applications/${id}`, { method: "DELETE" });
    await Promise.all([loadApplications(), loadJobs()]);
  }
}

async function addApplication() {
  const body = { company: $("#newCompany").value, title: $("#newTitle").value, url: $("#newUrl").value, applied_at: $("#newDate").value || null };
  try {
    await api("/api/applications", { method: "POST", body });
    for (const id of ["newCompany", "newTitle", "newUrl", "newDate"]) $("#" + id).value = "";
    await loadApplications();
  } catch (e) {
    alert(e.message);
  }
}

// --- onglets --------------------------------------------------------------------------------

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  for (const tab of ["offres", "suivi", "sources"]) $(`#tab-${tab}`).hidden = name !== tab;
}

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
$("#refreshBtn").addEventListener("click", () => refresh());
$("#companies").addEventListener("click", onCompaniesClick);
$("#sourcesBody").addEventListener("change", onSourcesChange);
$("#sourcesBody").addEventListener("click", onSourcesClick);
$("#detectBtn").addEventListener("click", detect);
$("#detectInput").addEventListener("keydown", (e) => { if (e.key === "Enter") detect(); });
$("#appsBody").addEventListener("change", onAppChange);
$("#appsBody").addEventListener("click", onAppClick);
$("#addAppBtn").addEventListener("click", addApplication);
$("#excludedList").addEventListener("click", onExcludedClick);
bindFilters();
loadState();
loadJobs();
loadSources();
loadApplications();

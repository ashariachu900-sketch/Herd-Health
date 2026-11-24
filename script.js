/**
 * CattleCare | Sustainable Farm Management
 * Full-Stack Client Script
 * Communicates with Python/SQLite Backend REST API with offline localStorage fallback
 */

(() => { // IIFE — prevents globals from leaking into the page scope

const STORAGE_KEY = "cattlecare_solarpunk_v1";
const API_BASE = window.location.protocol.startsWith("http") ? "" : "http://localhost:8000";

/** Milliseconds in one day — avoids repeating the magic number 86400000. */
const MS_PER_DAY = 86_400_000;

/** Only allow small data-URI images to be injected into an <img src>. */
const SAFE_PHOTO = /^(data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+|\/uploads\/[0-9a-f-]+\.(png|jpg|jpeg|gif|webp))$/;

// -----------------------------------------------------------------------------
// Admin Token — stored in sessionStorage; prompts once per browser session
// -----------------------------------------------------------------------------

/**
 * Returns the farm admin passcode from sessionStorage or the top-bar input.
 * No blocking prompt(): the user types the passcode once per browser session.
 * @returns {string} The admin token, or "" if it has not been entered yet.
 */
function getAdminToken() {
  const cached = sessionStorage.getItem("farm_admin_token");
  if (cached) return cached;

  const input = $("adminTokenInput");
  const token = (input?.value || "").trim();
  if (!token) {
    input?.focus();
    showToast("Enter the admin passcode in the top bar first.");
    return "";
  }
  sessionStorage.setItem("farm_admin_token", token);
  return token;
}

/** Forget a rejected token so the user can correct it without reloading. */
function clearAdminToken() {
  sessionStorage.removeItem("farm_admin_token");
  const input = $("adminTokenInput");
  if (input) {
    input.value = "";
    input.focus();
  }
}

/** Check silently: page loaders must not prompt before the user tries a write. */
function hasAdminToken() {
  return Boolean(sessionStorage.getItem("farm_admin_token") || ($("adminTokenInput")?.value || "").trim());
}

const PAGE_SIZE = 50;
let animals = [];
let isBackendConnected = false;
let currentPage = 0;
let totalCount = 0;
let dashboardStats = null;
let analytics = null;
let careEvents = [];
let auditEntries = [];
let toastTimer;

const $ = (id) => document.getElementById(id);

// -----------------------------------------------------------------------------
// Backend Synchronization & Local Storage Fallback
// -----------------------------------------------------------------------------

function updateConnectionUI(connected) {
  isBackendConnected = connected;
  const dot = $("liveDot");
  const label = $("liveStatus");

  if (!dot || !label) return;

  if (connected) {
    dot.classList.remove("offline");
    label.textContent = "Backend active (SQLite)";
  } else {
    dot.classList.add("offline");
    label.textContent = "Local mode (Offline)";
  }
}

function loadLocalAnimals() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(saved) ? saved : [];
  } catch {
    return [];
  }
}

function saveLocalAnimals() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(animals));
    return true;
  } catch (error) {
    showToast("Storage is full. Try using smaller photos.");
    return false;
  }
}

/** Fetch photo bytes only for records shown with a photo but no loaded image. */
async function ensurePhoto(animal) {
  if (!animal || animal.photo || !animal.hasPhoto || !isBackendConnected) return;
  try {
    const res = await fetch(`${API_BASE}/api/animals/${encodeURIComponent(animal.id)}`);
    if (res.ok) {
      const full = await res.json();
      animal.photo = full.photo || "";
    }
  } catch {
    // Leave the placeholder in place; the record itself is still readable.
  }
}

/** Hydrate photos after a summary list load, then redraw once. */
async function hydratePhotos() {
  const needed = animals.filter(a => a.hasPhoto && !a.photo);
  if (!needed.length) return;
  await Promise.all(needed.map(ensurePhoto));
  renderAll();
}

/** Dashboard aggregates come from the server so paging does not skew the totals. */
async function loadStats() {
  try {
    const res = await fetch(`${API_BASE}/api/stats`);
    if (res.ok) dashboardStats = await res.json();
  } catch {
    // Keep the last known stats while offline.
  }
}

async function syncAnimals(page = 0) {
  const params = new URLSearchParams({
    summary: "true",
    limit: String(PAGE_SIZE),
    offset: String(page * PAGE_SIZE)
  });
  const search = ($("cattleSearch")?.value || "").trim();
  if (search) params.set("search", search);
  const health = $("healthFilter")?.value || "all";
  if (health !== "all") params.set("health", health);

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3500);
    const response = await fetch(`${API_BASE}/api/animals?${params}`, {
      signal: controller.signal
    });
    clearTimeout(timeout);

    if (response.ok) {
      animals = await response.json();
      totalCount = Number(response.headers.get("X-Total-Count") || animals.length);
      currentPage = page;
      saveLocalAnimals(); // Keep localStorage in sync as backup cache
      updateConnectionUI(true);
      await loadStats();
      return;
    }
  } catch (error) {
    // Backend offline or unreachable
  }

  // Fallback to local storage
  updateConnectionUI(false);
  animals = loadLocalAnimals();
  totalCount = animals.length;
  currentPage = 0;
  dashboardStats = null;
}

function renderPagination() {
  const nav = $("pagination");
  if (!nav) return;
  const pages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));
  if (pages === 1) {
    nav.innerHTML = "";
    return;
  }
  nav.innerHTML = `
    <button type="button" data-page="${currentPage - 1}" ${currentPage === 0 ? "disabled" : ""} aria-label="Previous page">← Previous</button>
    <span>Page ${currentPage + 1} of ${pages}</span>
    <button type="button" data-page="${currentPage + 1}" ${currentPage >= pages - 1 ? "disabled" : ""} aria-label="Next page">Next →</button>
  `;
}

// -----------------------------------------------------------------------------
// Insights, care timeline and audit trail
// -----------------------------------------------------------------------------

async function loadAnalytics() {
  try {
    const res = await fetch(`${API_BASE}/api/analytics`);
    if (res.ok) analytics = await res.json();
  } catch {
    // Keep previous figures when offline.
  }
}

function insightBars(mapping) {
  const entries = Object.entries(mapping || {});
  if (!entries.length) return `<p class="empty-state">No data yet.</p>`;
  const max = Math.max(...entries.map(([, value]) => value), 1);
  return entries.map(([label, value]) => `
    <div class="insight-row">
      <span>${escapeHTML(label.replaceAll("_", " "))}</span>
      <span class="insight-bar"><i style="width:${Math.round(value / max * 100)}%"></i></span>
      <strong>${value}</strong>
    </div>
  `).join("");
}

function renderInsights() {
  const grid = $("insightGrid");
  if (!grid || !analytics) return;
  const breeds = (analytics.topBreeds || []).map(b => [b.breed, b.count]);
  grid.innerHTML = `
    <article class="insight-card"><h3>Health mix</h3>${insightBars(analytics.health)}</article>
    <article class="insight-card"><h3>Vaccine status</h3>${insightBars(analytics.vaccines)}</article>
    <article class="insight-card"><h3>Care events recorded</h3>${insightBars(analytics.eventTypes)}</article>
    <article class="insight-card">
      <h3>Verification & breeds</h3>
      <p>${analytics.verifiedCount} of ${analytics.total} records vet-verified.</p>
      ${breeds.length ? insightBars(Object.fromEntries(breeds)) : "<p>No breed data yet.</p>"}
    </article>
  `;
}

async function loadCareData() {
  const select = $("eventAnimal");
  if (!select) return;
  const previous = select.value;
  try {
    const res = await fetch(`${API_BASE}/api/animals?summary=true&limit=200`);
    const list = res.ok ? await res.json() : animals;
    select.innerHTML = list.length
      ? list.map(a => `<option value="${escapeHTML(a.id)}">${escapeHTML(a.tag)} — ${escapeHTML(a.name)}</option>`).join("")
      : `<option value="">No animals registered</option>`;
    if (list.some(a => a.id === previous)) select.value = previous;
  } catch {
    select.innerHTML = `<option value="">Backend unavailable</option>`;
  }
  await loadCareEvents();
  await loadAudit();
}

async function loadCareEvents() {
  const id = $("eventAnimal")?.value;
  if (!id || !hasAdminToken()) {
    careEvents = [];
    renderEvents();
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/animals/${encodeURIComponent(id)}/events`, {
      headers: { "X-Admin-Token": getAdminToken() }
    });
    if (res.status === 401) { clearAdminToken(); return; }
    careEvents = res.ok ? await res.json() : [];
  } catch {
    careEvents = [];
  }
  renderEvents();
}

function renderEvents() {
  const list = $("eventTimeline");
  if (!list) return;
  list.innerHTML = careEvents.length ? careEvents.map(event => `
    <div class="event-item">
      <strong>${escapeHTML(event.title)}</strong>
      <small>${escapeHTML(event.type.replaceAll("_", " "))} · ${escapeHTML(event.occurredAt)} · ${escapeHTML(event.actor)}</small>
      ${event.details ? `<div>${escapeHTML(event.details)}</div>` : ""}
    </div>
  `).join("") : `<div class="empty-state">🌱 No care events recorded for this animal.</div>`;
}

async function loadAudit() {
  const list = $("auditList");
  if (!list || !hasAdminToken()) {
    auditEntries = [];
    renderAudit();
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/audit`, {
      headers: { "X-Admin-Token": getAdminToken() }
    });
    if (res.status === 401) { clearAdminToken(); return; }
    auditEntries = res.ok ? await res.json() : [];
  } catch {
    auditEntries = [];
  }
  renderAudit();
}

function renderAudit() {
  const list = $("auditList");
  if (!list) return;
  list.innerHTML = auditEntries.length ? auditEntries.slice(0, 30).map(entry => `
    <div class="audit-item">
      <strong>${escapeHTML(entry.action)}</strong> — ${escapeHTML(entry.summary)}
      <small>${escapeHTML(entry.createdAt)} · ${escapeHTML(entry.actor)}</small>
    </div>
  `).join("") : `<div class="empty-state">No audited actions yet.</div>`;
}

// -----------------------------------------------------------------------------
// Utilities & Formatting
// -----------------------------------------------------------------------------

function escapeHTML(value = "") {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[char]);
}

function dateValue(value) {
  if (!value) return null;
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatDate(value) {
  const date = dateValue(value);
  return date ? date.toLocaleDateString(undefined, {
    day: "numeric", month: "short", year: "numeric"
  }) : "—";
}

function dayDifference(value) {
  const date = dateValue(value);
  if (!date) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((date - today) / MS_PER_DAY);
}

function vaccineStatus(value) {
  const days = dayDifference(value);
  if (days === null) return "none";
  if (days < 0) return "overdue";
  if (days === 0) return "today";
  return "upcoming";
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2800);
}

// -----------------------------------------------------------------------------
// Modal & Image Processing
// -----------------------------------------------------------------------------

let lastFocusedElement = null;

async function openModal(animal = null) {
  lastFocusedElement = document.activeElement;
  if (animal) await ensurePhoto(animal);
  $("animalForm").reset();
  $("animalId").value = animal?.id || "";
  $("modalTitle").textContent = animal ? "Update cattle details" : "Register cattle";
  $("animalName").value = animal?.name || "";
  $("animalTag").value = animal?.tag || "";
  $("animalBreed").value = animal?.breed || "";
  $("animalDob").value = animal?.dob || "";
  $("animalHealth").value = animal?.health || "Healthy";
  $("lastVaccineDate").value = animal?.lastVaccineDate || "";
  $("vaccineName").value = animal?.vaccineName || "";
  $("nextVaccineDate").value = animal?.nextVaccineDate || "";
  $("doctorNotes").value = animal?.notes || "";
  $("formMessage").textContent = "";
  $("animalModal").classList.add("open");
  $("animalModal").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  $("animalName").focus();
}

function closeModal() {
  $("animalModal").classList.remove("open");
  $("animalModal").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
  if (lastFocusedElement && typeof lastFocusedElement.focus === "function") {
    lastFocusedElement.focus();
  }
  lastFocusedElement = null;
}

function resizePhoto(file) {
  return new Promise((resolve, reject) => {
    if (!file) return resolve("");
    if (!file.type.startsWith("image/")) {
      return reject(new Error("Please choose an image file."));
    }

    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Could not read the image."));
    reader.onload = () => {
      const image = new Image();
      image.onerror = () => reject(new Error("Could not process the image."));
      image.onload = () => {
        const maxSize = 850;
        const scale = Math.min(1, maxSize / Math.max(image.width, image.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(image.width * scale);
        canvas.height = Math.round(image.height * scale);
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL("image/jpeg", 0.76));
      };
      image.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

// -----------------------------------------------------------------------------
// Form Handling (Create / Update via REST API)
// -----------------------------------------------------------------------------

$("animalForm").addEventListener("submit", async (event) => {
  event.preventDefault();

  const existingId = $("animalId").value;
  const existing = animals.find(animal => animal.id === existingId);
  const tag = $("animalTag").value.trim();
  const name = $("animalName").value.trim();

  if (!name || !tag) {
    $("formMessage").textContent = "Animal name and ID are required.";
    return;
  }

  // Client-side duplicate check
  const duplicate = animals.some(animal =>
    animal.tag.toLowerCase() === tag.toLowerCase() && animal.id !== existingId
  );

  if (duplicate) {
    $("formMessage").textContent = "This animal ID is already registered.";
    return;
  }

  const photoFile = $("animalPhoto").files[0];
  let photo = existing?.photo || "";

  try {
    if (photoFile) photo = await resizePhoto(photoFile);
  } catch (error) {
    $("formMessage").textContent = error.message;
    return;
  }

  const payload = {
    name,
    tag,
    breed: $("animalBreed").value.trim(),
    dob: $("animalDob").value,
    health: $("animalHealth").value,
    lastVaccineDate: $("lastVaccineDate").value,
    vaccineName: $("vaccineName").value.trim(),
    nextVaccineDate: $("nextVaccineDate").value,
    notes: $("doctorNotes").value.trim(),
    photo
  };

  // Offline writes are disabled on purpose: a local-only edit would be silently
  // overwritten by syncAnimals() when the server comes back.
  if (!isBackendConnected) {
    $("formMessage").textContent = "Offline: saving is disabled so records cannot be lost.";
    return;
  }

  const submitBtn = $("animalForm").querySelector("button[type='submit']");
  if (submitBtn) submitBtn.disabled = true;

  try {
    // Send to Python REST API with Admin Token
    const url = existingId ? `${API_BASE}/api/animals/${existingId}` : `${API_BASE}/api/animals`;
    const method = existingId ? "PUT" : "POST";

    const res = await fetch(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Admin-Token": getAdminToken()
      },
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (res.status === 401) {
      clearAdminToken();
      $("formMessage").textContent = "Wrong admin passcode. Re-enter it and try again.";
      return;
    }
    if (!res.ok) {
      $("formMessage").textContent = data.error || "Failed to save animal record.";
      return;
    }

    await syncAnimals(currentPage);
    renderAll();
    await hydratePhotos();

    closeModal();
    renderAll();
    showToast(existing ? "Cattle details updated 🌿" : "New cattle registered successfully 🌱");
  } catch (err) {
    $("formMessage").textContent = "Error saving animal: " + err.message;
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
});

// -----------------------------------------------------------------------------
// Deleting an Animal
// -----------------------------------------------------------------------------

async function deleteAnimal(id) {
  const animal = animals.find(a => a.id === id);
  if (!animal) return;

  if (!confirm(`Delete ${animal.name} (${animal.tag})? This cannot be undone.`)) return;

  if (!isBackendConnected) {
    showToast("Offline: deleting is disabled so records cannot be lost.");
    return;
  }

  try {
    const res = await fetch(`${API_BASE}/api/animals/${id}`, {
      method: "DELETE",
      headers: { "X-Admin-Token": getAdminToken() }
    });
    if (res.status === 401) {
      clearAdminToken();
      showToast("Wrong admin passcode. Re-enter it and try again.");
      return;
    }
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      showToast("Delete failed: " + (errData.error || "Server error"));
      return;
    }
    await syncAnimals(currentPage);
    renderAll();
    await hydratePhotos();
    showToast("Animal record deleted from database.");
  } catch (e) {
    showToast("Network error deleting animal: " + e.message);
  }
}

// -----------------------------------------------------------------------------
// Rendering Functions
// -----------------------------------------------------------------------------

function safePhotoUrl(photo) {
  if (!photo || typeof photo !== "string") return "";
  const trimmed = photo.trim();
  return SAFE_PHOTO.test(trimmed) ? trimmed : "";
}

function animalPhotoMarkup(animal, mini = false) {
  const photo = safePhotoUrl(animal.photo);
  if (photo) {
    return `<img src="${escapeHTML(photo)}" alt="${escapeHTML(animal.name)}">`;
  }
  return `<div class="${mini ? "mini-placeholder" : "photo-placeholder"}">🐄</div>`;
}

function renderDashboard() {
  const total = dashboardStats?.totalCattle ?? animals.length;
  const healthy = dashboardStats?.healthyCattle ?? animals.filter(a => a.health === "Healthy").length;
  const attention = dashboardStats?.attentionCattle ?? total - healthy;
  const due = dashboardStats?.dueVaccinations ?? animals.filter(a => {
    const status = vaccineStatus(a.nextVaccineDate);
    return status === "today" || status === "overdue";
  }).length;
  const vaccineCount = dashboardStats?.totalVaccines ?? animals.filter(a => a.vaccineName || a.lastVaccineDate || a.nextVaccineDate).length;
  const percentage = total ? Math.round(healthy / total * 100) : 0;

  $("totalCattle").textContent = total;
  $("healthyCattle").textContent = healthy;
  $("dueVaccinations").textContent = due;
  $("totalVaccines").textContent = vaccineCount;
  $("healthPercent").textContent = `${percentage}%`;
  $("legendHealthy").textContent = healthy;
  $("legendAttention").textContent = attention;
  $("healthRing").style.background =
    `conic-gradient(#5b8c61 ${percentage}%, #e8eee2 ${percentage}% 100%)`;

  const upcoming = (dashboardStats?.upcoming ?? animals
    .filter(a => a.nextVaccineDate && vaccineStatus(a.nextVaccineDate) !== "none"))
    .slice()
    .sort((a, b) => (a.nextVaccineDate || "").localeCompare(b.nextVaccineDate || ""))
    .slice(0, 4);

  $("upcomingList").innerHTML = upcoming.length ? upcoming.map(animal => {
    const status = vaccineStatus(animal.nextVaccineDate);
    const date = dateValue(animal.nextVaccineDate);
    const label = status === "overdue" ? "Overdue" : status === "today" ? "Due today" : "Upcoming";
    return `
      <div class="upcoming-item">
        <div class="upcoming-date">
          <strong>${date.getDate()}</strong>
          <small>${date.toLocaleDateString(undefined, { month: "short" })}</small>
        </div>
        <div class="upcoming-info">
          <strong>${escapeHTML(animal.name)} · ${escapeHTML(animal.vaccineName || "Vaccination")}</strong>
          <small>ID: ${escapeHTML(animal.tag)}</small>
        </div>
        <span class="status-pill ${status === "overdue" ? "overdue" : ""}">${label}</span>
      </div>`;
  }).join("") : `
    <div class="empty-state">🌱<strong>Nothing scheduled yet</strong>
      <span>Add a vaccination date to see reminders here.</span>
    </div>`;

  const recent = dashboardStats?.recent ?? animals.slice(0, 3);
  $("recentCattle").innerHTML = recent.length ? recent.map(animal => `
    <div class="mini-animal">
      ${animalPhotoMarkup(animal, true)}
      <div><strong>${escapeHTML(animal.name)}</strong><small>${escapeHTML(animal.tag)} · ${escapeHTML(animal.breed || "Breed not set")}</small></div>
    </div>`).join("") : `
      <div class="empty-state">🐄<strong>Your herd starts here</strong>
      <span>Register your first animal to see it here.</span></div>`;
}

function renderCattle() {
  // Search, health filter and paging are applied by the server before we get here.
  const start = totalCount ? currentPage * PAGE_SIZE + 1 : 0;
  $("cattleCount").textContent = totalCount
    ? `Showing ${start}–${start + animals.length - 1} of ${totalCount} animals`
    : "0 animals";

  $("cattleGrid").innerHTML = animals.length ? animals.map(animal => `
    <article class="animal-card">
      <div class="animal-photo">
        ${animalPhotoMarkup(animal)}
        <span class="status-pill ${animal.health === "Healthy" ? "done" : "overdue"}">${escapeHTML(animal.health)}</span>
      </div>
      <div class="animal-details">
        <h3>${escapeHTML(animal.name)}</h3>
        <div class="animal-meta">${escapeHTML(animal.tag)} · ${escapeHTML(animal.breed || "Breed not set")}</div>
        <div class="animal-notes">${escapeHTML(animal.notes || "No doctor notes have been added.")}</div>
        <div class="animal-meta">Next vaccine: ${formatDate(animal.nextVaccineDate)}</div>
        <div class="animal-actions">
          <button class="small-button" data-action="edit" data-id="${escapeHTML(animal.id)}">Edit details</button>
          <button class="small-button" data-action="qr" data-id="${escapeHTML(animal.id)}">View QR</button>
          <a class="small-button" href="portal.html?tag=${encodeURIComponent(animal.tag)}" target="_blank" style="text-decoration:none; display:inline-flex; align-items:center;">Passport ↗</a>
          <button class="small-button danger" data-action="delete" data-id="${escapeHTML(animal.id)}">Delete</button>
        </div>
      </div>
    </article>
  `).join("") : `
    <div class="empty-state">🌿<strong>No matching cattle found</strong>
    <span>Try another search or register a new animal.</span></div>`;
}

function renderVaccinations() {
  const records = animals.filter(a => a.vaccineName || a.lastVaccineDate || a.nextVaccineDate);
  const overdue = records.filter(a => vaccineStatus(a.nextVaccineDate) === "overdue").length;
  const upcoming = records.filter(a => vaccineStatus(a.nextVaccineDate) === "upcoming").length;
  const completed = records.filter(a => a.lastVaccineDate).length;

  $("vaccineSummary").innerHTML = `
    <div class="vaccine-stat"><span>Vaccination entries</span><strong>${records.length}</strong></div>
    <div class="vaccine-stat"><span>Upcoming vaccinations</span><strong>${upcoming}</strong></div>
    <div class="vaccine-stat"><span>Overdue vaccinations</span><strong>${overdue}</strong></div>
  `;

  $("vaccineTable").innerHTML = records.length ? records.map(animal => {
    const status = vaccineStatus(animal.nextVaccineDate);
    let label = "Not scheduled";
    let className = "";
    if (status === "overdue") { label = "Overdue"; className = "overdue"; }
    if (status === "today") { label = "Due today"; className = "overdue"; }
    if (status === "upcoming") { label = "Upcoming"; className = ""; }
    if (animal.lastVaccineDate && status === "none") { label = "Recorded"; className = "done"; }

    return `<tr>
      <td><strong>${escapeHTML(animal.name)}</strong><br><span class="animal-meta">${escapeHTML(animal.tag)}</span></td>
      <td>${escapeHTML(animal.vaccineName || "—")}</td>
      <td>${formatDate(animal.lastVaccineDate)}</td>
      <td>${formatDate(animal.nextVaccineDate)}</td>
      <td><span class="status-pill ${className}">${label}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="table-empty">No vaccination records yet.</td></tr>`;
}

function makeQrText(animal) {
  const host = window.location.protocol.startsWith("http") ? window.location.origin : "http://localhost:8000";
  // Keep the QR small and scannable; clinical details belong in the passport, not on a tag.
  return `${host}/portal.html?tag=${encodeURIComponent(animal.tag)}`;
}

function renderQrCodes() {
  const grid = $("qrGrid");

  if (!animals.length) {
    grid.innerHTML = `<div class="empty-state">▦<strong>No QR codes yet</strong>
      <span>Register cattle to create their individual QR codes.</span></div>`;
    return;
  }

  grid.innerHTML = animals.map(animal => `
    <article class="qr-card" data-qr-card="${escapeHTML(animal.id)}">
      <div class="eyebrow">CATTLECARE DIGITAL ID</div>
      <h3>${escapeHTML(animal.name)}</h3>
      <p>${escapeHTML(animal.tag)} · ${escapeHTML(animal.breed || "Breed not set")}</p>
      <div class="qr-image" id="qr-${escapeHTML(animal.id)}"></div>
      <button class="small-button" data-action="download-qr" data-id="${escapeHTML(animal.id)}">↓ Download QR code</button>
    </article>
  `).join("");

  animals.forEach(animal => {
    const container = document.getElementById(`qr-${animal.id}`);
    if (!container) return;

    if (typeof QRCode === "undefined") {
      container.textContent = "QR library unavailable. Connect to the internet and refresh.";
      return;
    }

    new QRCode(container, {
      text: makeQrText(animal),
      width: 145,
      height: 145,
      colorDark: "#214a35",
      colorLight: "#fffef9",
      correctLevel: QRCode.CorrectLevel.M
    });
  });
}

function renderAll() {
  renderDashboard();
  renderCattle();
  renderPagination();
  renderVaccinations();
  renderQrCodes();
}

function showPage(pageName) {
  const pageNames = {
    dashboard: "Overview",
    cattle: "My Cattle",
    vaccinations: "Vaccinations",
    qr: "QR Station",
    care: "Care Log",
    insights: "Insights"
  };

  document.querySelectorAll(".page").forEach(page => page.classList.remove("active"));
  document.querySelectorAll(".nav-link").forEach(link => {
    link.classList.toggle("active", link.dataset.page === pageName);
  });

  const page = document.getElementById(`${pageName}Page`);
  if (page) page.classList.add("active");

  $("pageCrumb").textContent = pageNames[pageName] || "Overview";
  $("sidebar").classList.remove("open");
  window.scrollTo({ top: 0, behavior: "smooth" });

  if (pageName === "qr") renderQrCodes();
  if (pageName === "care") loadCareData();
  if (pageName === "insights") { loadAnalytics().then(renderInsights); }
}

function downloadQr(id) {
  const animal = animals.find(a => a.id === id);
  const container = document.getElementById(`qr-${id}`);
  if (!animal || !container) return;

  const canvas = container.querySelector("canvas");
  const image = container.querySelector("img");
  let dataUrl = "";

  if (canvas) dataUrl = canvas.toDataURL("image/png");
  else if (image) dataUrl = image.src;

  if (!dataUrl) {
    showToast("QR code is not ready yet. Please try again.");
    return;
  }

  const link = document.createElement("a");
  const safeName = animal.tag.replace(/[^a-z0-9_-]/gi, "_");
  link.href = dataUrl;
  link.download = `${safeName}_QR_Code.png`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  showToast("QR code downloaded!");
}

// -----------------------------------------------------------------------------
// Event Listeners
// -----------------------------------------------------------------------------

// Navigation
document.querySelectorAll(".nav-link[data-page]").forEach(button => {
  button.addEventListener("click", () => showPage(button.dataset.page));
});

document.querySelectorAll("[data-go]").forEach(button => {
  button.addEventListener("click", () => showPage(button.dataset.go));
});

// Modal controls
$("openFormButton").addEventListener("click", () => openModal());
$("closeModal").addEventListener("click", closeModal);
$("cancelForm").addEventListener("click", closeModal);

$("animalModal").addEventListener("click", event => {
  if (event.target === $("animalModal")) closeModal();
});

document.addEventListener("keydown", event => {
  const modal = $("animalModal");
  if (!modal.classList.contains("open")) return;

  if (event.key === "Escape") {
    closeModal();
    return;
  }

  // Keep Tab focus inside the dialog while it is open.
  if (event.key === "Tab") {
    const focusable = [...modal.querySelectorAll("button, input, select, textarea, a[href]")]
      .filter(el => !el.disabled && el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});

// Cattle search and filtering now run on the server so paging stays correct.
let reloadTimer;
function reloadPage() {
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(async () => {
    await syncAnimals(0);
    renderAll();
    await hydratePhotos();
  }, 250);
}
$("cattleSearch").addEventListener("input", reloadPage);
$("healthFilter").addEventListener("change", reloadPage);

$("pagination")?.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-page]");
  if (!button || button.disabled) return;
  await syncAnimals(Number(button.dataset.page));
  renderAll();
  await hydratePhotos();
});

// Cattle card and QR actions
document.addEventListener("click", event => {
  const button = event.target.closest("[data-action]");
  if (!button) return;

  const { action, id } = button.dataset;
  const animal = animals.find(a => a.id === id);

  if (action === "edit" && animal) openModal(animal);
  if (action === "delete") deleteAnimal(id);

  if (action === "qr") {
    showPage("qr");
    setTimeout(() => {
      const card = document.querySelector(`[data-qr-card="${CSS.escape(id)}"]`);
      card?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 100);
  }

  if (action === "download-qr") downloadQr(id);
});

$("eventAnimal")?.addEventListener("change", loadCareEvents);
$("eventForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = getAdminToken();
  if (!token) return;
  const animalId = $("eventAnimal").value;
  const message = $("eventMessage");
  if (!animalId) {
    message.textContent = "Register an animal first.";
    return;
  }
  const payload = {
    type: $("eventType").value,
    title: $("eventTitle").value.trim(),
    occurredAt: $("eventDate").value,
    details: $("eventDetails").value.trim()
  };
  if (!payload.title || !payload.occurredAt) {
    message.textContent = "A title and date are required.";
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/animals/${encodeURIComponent(animalId)}/events`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Admin-Token": token },
      body: JSON.stringify(payload)
    });
    if (res.status === 401) {
      clearAdminToken();
      message.textContent = "Wrong admin passcode. Event not saved.";
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      message.textContent = body.error || "Could not save the event.";
      return;
    }
    $("eventTitle").value = "";
    $("eventDetails").value = "";
    message.textContent = "Care event added.";
    await loadCareEvents();
    await loadAudit();
    showToast("Care event recorded. ✎");
  } catch {
    message.textContent = "Backend unavailable. Event not saved.";
  }
});

// Mobile sidebar
$("menuToggle").addEventListener("click", () => {
  $("sidebar").classList.toggle("open");
});

// Export full database backup
const backupBtn = $("exportBackupBtn");
if (backupBtn) {
  backupBtn.addEventListener("click", async () => {
    const download = (data, fileName) => {
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    };
    const dateStr = new Date().toISOString().split("T")[0];

    let res;
    try {
      res = await fetch(`${API_BASE}/api/export`, {
        headers: { "X-Admin-Token": getAdminToken() }
      });
    } catch (err) {
      // The paged local cache holds only the last viewed page. Downloading it
      // would look like a complete backup while silently omitting other animals.
      showToast("Offline backup unavailable: connect to the server to export every record.");
      return;
    }

    if (res.status === 401) {
      clearAdminToken();
      showToast("Wrong admin passcode. Backup not downloaded.");
      return;
    }
    if (!res.ok) {
      showToast("Server export failed. Backup not downloaded.");
      return;
    }
    download(await res.json(), `cattlecare_backup_${dateStr}.json`);
    showToast("Herd database backup downloaded! 💾");
  });
}

// Initial date setup
$("todayDate").textContent = new Date().toLocaleDateString(undefined, {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric"
});
$("footerYear").textContent = new Date().getFullYear();

// Initialize app: sync with backend, then render
(async function init() {
  await syncAnimals();
  renderAll();
  await hydratePhotos();
})();

})(); // end IIFE

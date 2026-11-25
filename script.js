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

// Protected farmer/veterinarian sessions authorize writes and care records.
const PAGE_SIZE = 50;
let animals = [];
let isBackendConnected = false;
let currentPage = 0;
let totalCount = 0;
let dashboardStats = null;
let analytics = null;
let careEvents = [];
let toastTimer;
let sessionInfo = null;
const $ = (id) => document.getElementById(id);

function authHeaders() {
  return sessionInfo ? { "X-CSRF-Token": sessionInfo.csrfToken } : {};
}

function hasAdminSession() {
  return Boolean(sessionInfo && sessionInfo.role === "admin");
}

async function restoreSession() {
  try {
    const res = await fetch(`${API_BASE}/api/session`, { credentials: "same-origin" });
    if (res.ok) {
      const info = await res.json();
      sessionInfo = info.authenticated ? info : null;
    }
  } catch {
    sessionInfo = null;
  }
  updateSessionUI();
}

function updateSessionUI() {
  $("sessionLabel").textContent = sessionInfo
    ? `${sessionInfo.actor} · ${sessionInfo.role}`
    : "Signed out";
  $("loginBtn").hidden = Boolean(sessionInfo);
  $("loginRole").hidden = Boolean(sessionInfo);
  $("adminTokenInput").hidden = Boolean(sessionInfo);
  $("logoutBtn").hidden = !sessionInfo;
}

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

// -----------------------------------------------------------------------------
async function loadCareEvents() {
  const id = $("eventAnimal")?.value;
  if (!id || !hasAdminSession()) {
    careEvents = [];
    const list = $("eventTimeline");
    if (list) list.innerHTML = `<div class="empty-state">Sign in as a farmer to view care events.</div>`;
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/animals/${encodeURIComponent(id)}/events`, {
      credentials: "same-origin",
      headers: authHeaders()
    });
    careEvents = res.ok ? await res.json() : [];
  } catch {
    careEvents = [];
  }
  const list = $("eventTimeline");
  if (list) {
    list.innerHTML = careEvents.length ? careEvents.map(event => `
      <div class="event-item">
        <strong>${escapeHTML(event.title)}</strong>
        <small>${escapeHTML(event.animalTag || "")} · ${escapeHTML(event.animalName || "")} · ${escapeHTML(event.vaccineName || "No vaccination name")} · ${escapeHTML(event.type.replaceAll("_", " "))} · ${escapeHTML(event.occurredAt)} · ${escapeHTML(event.actor)}</small>
        ${event.details ? `<div>${escapeHTML(event.details)}</div>` : ""}
      </div>
    `).join("") : `<div class="empty-state">No care events recorded.</div>`;
  }
}

async function loadCareData() {
  const select = $("eventAnimal");
  if (!select) return;
  const previous = select.value;
  try {
    const res = await fetch(`${API_BASE}/api/animals?summary=true&limit=200`);
    const list = res.ok ? await res.json() : animals;
    select.innerHTML = list.length
      ? list.map(a => `<option value="${escapeHTML(a.id)}">${escapeHTML(a.tag)} — ${escapeHTML(a.name)} — ${escapeHTML(a.vaccineName || "No vaccine name")}</option>`).join("")
      : `<option value="">No animals registered</option>`;
    if (list.some(a => a.id === previous)) select.value = previous;
  } catch {
    select.innerHTML = `<option value="">Backend unavailable</option>`;
  }
  await loadCareEvents();
}

let lastFocusedElement = null;

async function openModal(animal = null) {
  if (!hasAdminSession()) {
    showToast("Sign in as a farmer first.");
    $("adminTokenInput").focus();
    return;
  }
  lastFocusedElement = document.activeElement;
  if (animal) await ensurePhoto(animal);
  $("animalForm").reset();
  $("animalId").value = animal?.id || "";
  $("modalTitle").textContent = animal ? "Update cattle details" : "Register cattle";
  $("animalName").value = animal?.name || "";
  $("animalTag").value = animal?.tag || "";
  $("animalTag").placeholder = animal ? "" : "Generated automatically";
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
  lastFocusedElement?.focus();
  lastFocusedElement = null;
}

document.addEventListener("keydown", event => {
  const modal = $("animalModal");
  if (!modal?.classList.contains("open")) return;
  if (event.key === "Escape") {
    closeModal();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...modal.querySelectorAll("button, input, select, textarea")]
    .filter(element => !element.disabled && element.offsetParent !== null);
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
});

function resizePhoto(file) {
  return new Promise((resolve, reject) => {
    if (!file) return resolve("");
    if (!file.type.startsWith("image/")) return reject(new Error("Please choose an image file."));
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Could not read the image."));
    reader.onload = () => {
      const image = new Image();
      image.onerror = () => reject(new Error("Could not process the image."));
      image.onload = () => {
        const scale = Math.min(1, 850 / Math.max(image.width, image.height));
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(image.width * scale);
        canvas.height = Math.round(image.height * scale);
        canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL("image/jpeg", 0.76));
      };
      image.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

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
        <div class="animal-meta"><strong>Vaccination:</strong> ${escapeHTML(animal.vaccineName || "Not recorded")}</div>
        <div class="animal-notes">${escapeHTML(animal.notes || "No doctor notes have been added.")}</div>
        <div class="animal-meta">Next vaccine: ${formatDate(animal.nextVaccineDate)}</div>
        <div class="animal-actions">
          <button class="small-button" data-action="edit" data-id="${escapeHTML(animal.id)}">Edit details</button>
          <button class="small-button" data-action="qr" data-id="${escapeHTML(animal.id)}">View QR</button>
          <a class="small-button" href="portal.html?tag=${encodeURIComponent(animal.tag)}&public=1" target="_blank" style="text-decoration:none; display:inline-flex; align-items:center;">Passport ↗</a>
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
      <td><strong>${escapeHTML(animal.name)}</strong><br><span class="animal-meta">${escapeHTML(animal.tag)} · ${escapeHTML(animal.vaccineName || "No vaccine name")}</span></td>
      <td>${escapeHTML(animal.vaccineName || "—")}</td>
      <td>${formatDate(animal.lastVaccineDate)}</td>
      <td>${formatDate(animal.nextVaccineDate)}</td>
      <td><span class="status-pill ${className}">${label}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="5" class="table-empty">No vaccination records yet.</td></tr>`;
}

function makeQrText(animal) {
  const host = window.location.protocol.startsWith("http") ? window.location.origin : "http://localhost:8000";
  // Keep the QR small and scannable; a scanned tag opens the safe passport only.
  return `${host}/portal.html?tag=${encodeURIComponent(animal.tag)}&public=1`;
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
      <p>${escapeHTML(animal.tag)} · ${escapeHTML(animal.vaccineName || "No vaccine name")} · ${escapeHTML(animal.breed || "Breed not set")}</p>
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
  link.download = `${safeName}_${animal.vaccineName ? animal.vaccineName.replace(/[^a-z0-9_-]/gi, "_") : "No_Vaccine"}_QR_Code.png`;
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

$("openFormButton")?.addEventListener("click", () => {
  if (!hasAdminSession()) {
    showToast("Sign in as a farmer first.");
    $("adminTokenInput").focus();
    return;
  }
  openModal();
});
$("closeModal")?.addEventListener("click", closeModal);
$("cancelForm")?.addEventListener("click", closeModal);
$("animalModal")?.addEventListener("click", event => {
  if (event.target === $("animalModal")) closeModal();
});

$("animalForm")?.addEventListener("submit", async event => {
  event.preventDefault();
  const existingId = $("animalId").value;
  const existing = animals.find(animal => animal.id === existingId);
  const name = $("animalName").value.trim();
  if (!name) {
    $("formMessage").textContent = "Animal name is required.";
    return;
  }
  let photo = existing?.photo || "";
  try {
    const file = $("animalPhoto").files[0];
    if (file) photo = await resizePhoto(file);
  } catch (error) {
    $("formMessage").textContent = error.message;
    return;
  }
  const payload = {
    name,
    tag: existing?.tag || "",
    breed: $("animalBreed").value.trim(),
    dob: $("animalDob").value,
    health: $("animalHealth").value,
    lastVaccineDate: $("lastVaccineDate").value,
    vaccineName: $("vaccineName").value.trim(),
    nextVaccineDate: $("nextVaccineDate").value,
    notes: $("doctorNotes").value.trim(),
    photo
  };
  if (!hasAdminSession()) {
    $("formMessage").textContent = "Sign in as a farmer first.";
    return;
  }
  const submitBtn = $("animalForm").querySelector("button[type='submit']");
  if (submitBtn) submitBtn.disabled = true;
  try {
    const res = await fetch(existingId ? `${API_BASE}/api/animals/${existingId}` : `${API_BASE}/api/animals`, {
      method: existingId ? "PUT" : "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(payload)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("formMessage").textContent = data.error || "Failed to save animal record.";
      return;
    }
    await syncAnimals(currentPage);
    renderAll();
    await hydratePhotos();
    closeModal();
    showToast(existing ? "Cattle details updated." : "New cattle registered.");
  } catch (error) {
    $("formMessage").textContent = "Error saving animal: " + error.message;
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
});

async function deleteAnimal(id) {
  const animal = animals.find(a => a.id === id);
  if (!animal || !confirm(`Delete ${animal.name} (${animal.tag})? This cannot be undone.`)) return;
  if (!hasAdminSession()) {
    showToast("Sign in as a farmer first.");
    $("adminTokenInput").focus();
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/animals/${id}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers: authHeaders()
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showToast(body.error || "Delete failed.");
      return;
    }
    await syncAnimals(currentPage);
    renderAll();
    showToast("Animal record deleted.");
  } catch (error) {
    showToast("Network error deleting animal: " + error.message);
  }
}

$("eventAnimal")?.addEventListener("change", loadCareEvents);
$("eventForm")?.addEventListener("submit", async event => {
  event.preventDefault();
  if (!hasAdminSession()) {
    $("eventMessage").textContent = "Sign in as a farmer first.";
    return;
  }
  const animalId = $("eventAnimal").value;
  const payload = {
    type: $("eventType").value,
    title: $("eventTitle").value.trim(),
    vaccineName: $("eventVaccineName")?.value.trim() || "",
    occurredAt: $("eventDate").value,
    nextVaccineDate: $("eventNextVaccineDate")?.value || "",
    details: $("eventDetails").value.trim()
  };
  if (!animalId || !payload.title || !payload.occurredAt) {
    $("eventMessage").textContent = "Choose an animal and enter a title and date.";
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/animals/${encodeURIComponent(animalId)}/events`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(payload)
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("eventMessage").textContent = body.error || "Could not save the event.";
      return;
    }
    $("eventTitle").value = "";
    $("eventDetails").value = "";
    $("eventMessage").textContent = "Care event added.";
    await loadCareEvents();
  } catch {
    $("eventMessage").textContent = "Backend unavailable. Event not saved.";
  }
});

// Mobile sidebar
$("menuToggle").addEventListener("click", () => {
  $("sidebar").classList.toggle("open");
});

// Initial date setup
$("todayDate").textContent = new Date().toLocaleDateString(undefined, {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric"
});
$("footerYear").textContent = new Date().getFullYear();

$("loginBtn")?.addEventListener("click", async () => {
  const token = $("adminTokenInput").value.trim();
  if (!token) {
    showToast("Enter the account passcode.");
    $("adminTokenInput").focus();
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/session`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token
      })
    });
    if (!res.ok) {
      showToast("Invalid passcode.");
      return;
    }
    sessionInfo = await res.json();
    if (sessionInfo.role !== $("loginRole").value) {
      await fetch(`${API_BASE}/api/logout`, { method: "POST", credentials: "same-origin" });
      sessionInfo = null;
      updateSessionUI();
      showToast("The passcode does not match the selected role.");
      return;
    }
    $("adminTokenInput").value = "";
    updateSessionUI();
    showToast(`Signed in as ${sessionInfo.actor}.`);
    if (sessionInfo.role === "vet") {
      window.location.href = "portal.html";
    }
  } catch {
    showToast("Sign-in failed: backend unavailable.");
  }
});

$("logoutBtn")?.addEventListener("click", async () => {
  try {
    await fetch(`${API_BASE}/api/logout`, {
      method: "POST",
      credentials: "same-origin",
      headers: sessionInfo ? { "X-CSRF-Token": sessionInfo.csrfToken } : {}
    });
  } catch {}
  sessionInfo = null;
  updateSessionUI();
  showToast("Signed out.");
});

// Initialize app: sync with backend, then render
(async function init() {
  await restoreSession();
  await syncAnimals();
  renderAll();
  await hydratePhotos();
})();

})(); // end IIFE

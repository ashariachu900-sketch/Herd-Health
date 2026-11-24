/**
 * CattleCare | Public & Vet Health Verification Portal Script
 * Handles herd inspection, live search, filtering, and single animal passport generation
 */

(() => { // IIFE — prevents globals from leaking into the page scope

const STORAGE_KEY = "cattlecare_solarpunk_v1";
const API_BASE = window.location.protocol.startsWith("http") ? "" : "http://localhost:8000";

/** Milliseconds in one day — avoids repeating the magic number 86400000. */
const MS_PER_DAY = 86_400_000;

/** Only allow small data-URI images to be injected into an <img src>. */
const SAFE_PHOTO = /^(data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+|\/uploads\/[0-9a-f-]+\.(png|jpg|jpeg|gif|webp))$/;

let animals = [];
let isBackendConnected = false;
let currentFilter = "all";
let toastTimer;

const $ = (id) => document.getElementById(id);

// -----------------------------------------------------------------------------
// Initialization & Data Loading
// -----------------------------------------------------------------------------

async function init() {
  await syncAnimals();
  setupEventListeners();

  // Check if URL specifies an animal ID or Tag (e.g., ?id=... or ?tag=COW-101)
  const urlParams = new URLSearchParams(window.location.search);
  const animalId = urlParams.get("id");
  const animalTag = urlParams.get("tag");

  if (animalTag) {
    const found = animals.find(a => (a.tag || "").toLowerCase() === animalTag.toLowerCase());
    if (found) {
      await showPassport(found);
      return;
    }
    showMissingRecord(animalTag);
    return;
  } else if (animalId) {
    const found = animals.find(a => a.id === animalId);
    if (found) {
      await showPassport(found);
      return;
    }
    showMissingRecord(animalId);
    return;
  }

  // Otherwise, render full herd directory
  renderDirectory();
  await hydratePhotos();
}

function showMissingRecord(key) {
  renderDirectory();
  const notice = $("missingRecord");
  notice.textContent = `No animal record found for "${key}". The record may have been deleted or the link may be incorrect.`;
  notice.hidden = false;
}

async function syncAnimals() {
  const dot = $("portalStatusDot");
  const label = $("portalStatusLabel");

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3500);

    const res = await fetch(`${API_BASE}/api/animals?summary=true&limit=200`, { signal: controller.signal });
    clearTimeout(timeout);

    if (res.ok) {
      animals = await res.json();
      isBackendConnected = true;
      dot.classList.remove("offline");
      label.textContent = "Live Farm Database";
      return;
    }
  } catch (err) {
    // Offline or server not running
  }

  // Fallback to localStorage
  isBackendConnected = false;
  dot.classList.add("offline");
  label.textContent = "Local Offline Cache";
  try {
    animals = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  } catch {
    animals = [];
  }
}

/** Load the full record only when a directory card with a photo is actually shown. */
async function ensurePhoto(animal) {
  if (!animal || animal.photo || !animal.hasPhoto || !isBackendConnected) return;
  try {
    const res = await fetch(`${API_BASE}/api/animals/${encodeURIComponent(animal.id)}`);
    if (res.ok) {
      const full = await res.json();
      animal.photo = full.photo || "";
    }
  } catch {
    // Keep the cow placeholder; the rest of the passport still renders.
  }
}

/** Hydrate photos after a summary list load, then redraw the directory once. */
async function hydratePhotos() {
  const needed = animals.filter(a => a.hasPhoto && !a.photo);
  if (!needed.length) return;
  await Promise.all(needed.map(ensurePhoto));
  renderDirectory();
}

// -----------------------------------------------------------------------------
// Utilities & Formatting
// -----------------------------------------------------------------------------

function escapeHTML(str = "") {
  return String(str).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}

function formatDate(val) {
  if (!val) return "Not recorded";
  const date = new Date(`${val}T00:00:00`);
  return Number.isNaN(date.getTime()) ? "Not recorded" : date.toLocaleDateString(undefined, {
    day: "numeric", month: "short", year: "numeric"
  });
}

function safePhotoUrl(photo) {
  if (!photo || typeof photo !== "string") return "";
  const trimmed = photo.trim();
  return SAFE_PHOTO.test(trimmed) ? trimmed : "";
}

function calculateAge(dobStr) {
  if (!dobStr) return "Not recorded";
  const birth = new Date(`${dobStr}T00:00:00`);
  if (Number.isNaN(birth.getTime())) return "Not recorded";

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  if (birth > today) return "Invalid (Future Date)";

  let years = today.getFullYear() - birth.getFullYear();
  let months = today.getMonth() - birth.getMonth();

  if (months < 0 || (months === 0 && today.getDate() < birth.getDate())) {
    years--;
    months += 12;
  }

  if (years <= 0 && months <= 0) return "Newborn";
  if (years <= 0) return `${months} month${months > 1 ? "s" : ""}`;
  if (months === 0) return `${years} year${years > 1 ? "s" : ""}`;
  return `${years} yr${years > 1 ? "s" : ""} ${months} mo${months > 1 ? "s" : ""}`;
}

function dayDifference(dateStr) {
  if (!dateStr) return null;
  const target = new Date(`${dateStr}T00:00:00`);
  if (Number.isNaN(target.getTime())) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((target - today) / MS_PER_DAY);
}

function vaccineStatus(dateStr) {
  const days = dayDifference(dateStr);
  if (days === null) return { code: "none", label: "No upcoming date set" };
  if (days < 0) return { code: "overdue", label: `Overdue by ${Math.abs(days)} day${Math.abs(days) > 1 ? "s" : ""}` };
  if (days === 0) return { code: "today", label: "Due Today" };
  return { code: "upcoming", label: `Due in ${days} day${days > 1 ? "s" : ""}` };
}

function showToast(message) {
  const toast = $("portalToast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2800);
}

// -----------------------------------------------------------------------------
// Directory Rendering & Metrics
// -----------------------------------------------------------------------------

function renderDirectory() {
  $("passportSection").classList.remove("active");
  $("directorySection").style.display = "block";
  document.body.classList.remove("passport-open");
  $("missingRecord").hidden = true;

  // Calculate Metrics
  const total = animals.length;
  const healthy = animals.filter(a => a.health === "Healthy").length;
  const attention = total - healthy;
  const due = animals.filter(a => {
    const st = vaccineStatus(a.nextVaccineDate).code;
    return st === "today" || st === "overdue";
  }).length;
  const healthRate = total ? Math.round((healthy / total) * 100) : 0;

  $("metricTotal").textContent = total;
  $("metricHealthyRate").textContent = `${healthRate}%`;
  $("metricAttention").textContent = attention;
  $("metricDue").textContent = due;

  // Filter animals
  const query = $("searchInput").value.trim().toLowerCase();
  const filtered = animals.filter(animal => {
    // Search match
    const matchSearch = [animal.name, animal.tag, animal.breed, animal.notes]
      .some(field => (field || "").toLowerCase().includes(query));

    // Pill filter match
    let matchFilter = true;
    if (currentFilter === "healthy") matchFilter = animal.health === "Healthy";
    if (currentFilter === "attention") matchFilter = animal.health !== "Healthy";
    if (currentFilter === "vaccine_due") {
      const st = vaccineStatus(animal.nextVaccineDate).code;
      matchFilter = st === "today" || st === "overdue";
    }

    return matchSearch && matchFilter;
  });

  $("resultsCount").textContent = `Showing ${filtered.length} animal${filtered.length === 1 ? "" : "s"}`;

  const grid = $("cattleGrid");
  if (!filtered.length) {
    grid.innerHTML = `
      <div style="grid-column: 1 / -1; background: var(--paper); padding: 40px; text-align: center; border-radius: 18px; border: 1px dashed var(--line);">
        <div style="font-size: 38px; margin-bottom: 8px;">🐄</div>
        <strong style="display: block; font-size: 16px; margin-bottom: 4px;">No matching cattle records found</strong>
        <span style="color: var(--muted); font-size: 12px;">Try adjusting your search criteria or filter pills.</span>
      </div>
    `;
    return;
  }

  grid.innerHTML = filtered.map(animal => {
    const isHealthy = animal.health === "Healthy";
    const vStatus = vaccineStatus(animal.nextVaccineDate);

    const photoUrl = safePhotoUrl(animal.photo);

    return `
      <article class="cattle-card">
        <div class="card-img-wrap">
          ${photoUrl ? `<img src="${escapeHTML(photoUrl)}" alt="${escapeHTML(animal.name)}">` : `<div class="card-img-placeholder">🐄</div>`}
          <span class="card-health-pill ${isHealthy ? 'healthy' : 'attention'}">${escapeHTML(animal.health)}</span>
        </div>
        <div class="card-body">
          <span class="card-tag">${escapeHTML(animal.tag)}</span>
          <h3 class="card-name">${escapeHTML(animal.name)}</h3>
          <div class="card-meta">
            ${escapeHTML(animal.breed || "Breed not specified")} · Age: ${calculateAge(animal.dob)}
          </div>
          
          <div class="card-notes-preview">
            <strong>Doctor observations:</strong><br>
            ${escapeHTML(animal.notes || "No clinical observations entered by the farm manager.")}
          </div>

          <div style="font-size: 11px; color: #586959; margin-bottom: 12px;">
            <span>Next vaccine: </span><strong>${formatDate(animal.nextVaccineDate)}</strong>
            ${vStatus.code !== 'none' ? `<span style="font-size: 10px; color: ${vStatus.code === 'overdue' ? '#a33c2a' : '#2a5298'}; font-weight: 700;">(${vStatus.label})</span>` : ''}
          </div>

          <div class="card-actions">
            <button class="card-btn primary" data-passport-id="${escapeHTML(animal.id)}">
              View Full Passport ↗
            </button>
          </div>
        </div>
      </article>
    `;
  }).join("");
}

// -----------------------------------------------------------------------------
// Animal Passport View (Detailed Digital Certificate)
// -----------------------------------------------------------------------------

async function showPassport(animal) {
  await ensurePhoto(animal);
  $("directorySection").style.display = "none";
  $("missingRecord").hidden = true;
  document.body.classList.add("passport-open");
  const passport = $("passportSection");
  passport.classList.add("active");
  window.scrollTo({ top: 0, behavior: "smooth" });

  // Update URL query string without reloading page
  const newUrl = `${window.location.pathname}?tag=${encodeURIComponent(animal.tag)}`;
  window.history.pushState({ animalId: animal.id }, "", newUrl);

  // Photo
  const passportPhoto = safePhotoUrl(animal.photo);
  $("passportPhoto").innerHTML = passportPhoto ?
    `<img src="${escapeHTML(passportPhoto)}" alt="${escapeHTML(animal.name)}">` :
    `<div style="display:grid; place-items:center; height:100%; font-size:75px;">🐄</div>`;

  // Hero Info
  $("passportTag").textContent = animal.tag;
  $("passportName").textContent = animal.name;
  $("passportBreed").textContent = `${animal.breed || "Standard Breed"} · ${calculateAge(animal.dob)}`;

  // Badges
  const isHealthy = animal.health === "Healthy";
  const vStat = vaccineStatus(animal.nextVaccineDate);
  const isVaccineDue = vStat.code === "today" || vStat.code === "overdue";

  $("passportBadges").innerHTML = `
    <span class="badge-item ${isHealthy ? 'healthy' : 'attention'}">
      ${isHealthy ? '✓ Healthy Condition' : '⚠ Under Medical Attention'}
    </span>
    <span class="badge-item ${isVaccineDue ? 'vaccine-due' : 'vaccine-ok'}">
      ✚ Vaccination: ${vStat.label}
    </span>
  `;

  // Specifications Grid
  $("specTag").textContent = animal.tag;
  $("specBreed").textContent = animal.breed || "Not recorded";
  $("specDob").textContent = formatDate(animal.dob);
  $("specAge").textContent = calculateAge(animal.dob);
  $("specHealth").textContent = animal.health;
  $("specRegistered").textContent = animal.createdAt ? formatDate(animal.createdAt.split("T")[0]) : "Active record";

  // Vaccination Specifications
  $("specVaccineName").textContent = animal.vaccineName || "None scheduled";
  $("specLastVaccine").textContent = formatDate(animal.lastVaccineDate);
  $("specNextVaccine").textContent = formatDate(animal.nextVaccineDate);
  $("specVaccineStatus").textContent = vStat.label;

  // Doctor Notes
  $("specDoctorNotes").textContent = animal.notes || "No clinical observations or doctor notes have been entered for this animal.";

  // Verification status: never claim a record is verified unless a vet set the flag.
  const verified = Number(animal.verified) === 1;
  const stamp = document.querySelector(".certificate-stamp");
  stamp.textContent = verified ? "✓ Vet Verified Record" : "⚠ Farmer entry — not vet-verified";
  stamp.classList.toggle("unverified", !verified);
  $("certificateTimestamp").textContent = verified
    ? `Verified at: ${formatDate(String(animal.verifiedAt || "").split("T")[0])} by ${animal.verifiedBy || "a veterinarian"}`
    : "No veterinary verification on file";
}

function openPassportById(id) {
  const animal = animals.find(a => a.id === id);
  if (animal) showPassport(animal);
}

// -----------------------------------------------------------------------------
// Event Handlers
// -----------------------------------------------------------------------------

function setupEventListeners() {
  // Search
  $("searchInput").addEventListener("input", renderDirectory);
  $("searchButton").addEventListener("click", renderDirectory);

  // Cattle Grid Card Click Delegation
  $("cattleGrid").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-passport-id]");
    if (btn) {
      openPassportById(btn.dataset.passportId);
    }
  });

  // Filter Pills
  $("filterPills").addEventListener("click", (e) => {
    const btn = e.target.closest(".filter-pill");
    if (!btn) return;

    document.querySelectorAll(".filter-pill").forEach(p => {
      p.classList.remove("active");
      p.setAttribute("aria-pressed", "false");
    });
    btn.classList.add("active");
    btn.setAttribute("aria-pressed", "true");
    currentFilter = btn.dataset.filter;
    renderDirectory();
  });

  // Back to Directory
  $("backToDirectory").addEventListener("click", () => {
    window.history.pushState({}, "", window.location.pathname);
    hydratePhotos();
  });

  // Print Certificate
  $("printBtn").addEventListener("click", () => {
    window.print();
  });

  // Copy Direct Link
  $("copyLinkBtn").addEventListener("click", () => {
    const link = window.location.href;
    navigator.clipboard.writeText(link).then(() => {
      showToast("Passport link copied to clipboard! 📋");
    }).catch(() => {
      showToast("Link: " + link);
    });
  });

  // Popstate handler for browser back/forward buttons
  window.addEventListener("popstate", () => {
    const params = new URLSearchParams(window.location.search);
    const tag = params.get("tag");
    const id = params.get("id");
    if (tag) {
      const a = animals.find(item => (item.tag || "").toLowerCase() === tag.toLowerCase());
      if (a) return showPassport(a);
    }
    if (id) {
      const a = animals.find(item => item.id === id);
      if (a) return showPassport(a);
    }
    renderDirectory();
  });
}

document.addEventListener("DOMContentLoaded", init);

})(); // end IIFE


const STORAGE_KEY = "cattlecare_solarpunk_v1";

let animals = loadAnimals();
let toastTimer;

const $ = (id) => document.getElementById(id);

function loadAnimals() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(saved) ? saved : [];
  } catch {
    return [];
  }
}

function saveAnimals() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(animals));
    return true;
  } catch (error) {
    showToast("Storage is full. Try using smaller photos.");
    return false;
  }
}

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
  return Math.round((date - today) / 86400000);
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

function openModal(animal = null) {
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

$("animalForm").addEventListener("submit", async (event) => {
  event.preventDefault();

  const existingId = $("animalId").value;
  const existing = animals.find(animal => animal.id === existingId);
  const tag = $("animalTag").value.trim();

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

  const animal = {
    id: existingId || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())),
    name: $("animalName").value.trim(),
    tag,
    breed: $("animalBreed").value.trim(),
    dob: $("animalDob").value,
    health: $("animalHealth").value,
    lastVaccineDate: $("lastVaccineDate").value,
    vaccineName: $("vaccineName").value.trim(),
    nextVaccineDate: $("nextVaccineDate").value,
    notes: $("doctorNotes").value.trim(),
    photo,
    createdAt: existing?.createdAt || new Date().toISOString()
  };

  if (!animal.name || !animal.tag) {
    $("formMessage").textContent = "Animal name and ID are required.";
    return;
  }

  if (existing) {
    animals = animals.map(item => item.id === existingId ? animal : item);
  } else {
    animals.unshift(animal);
  }

  if (!saveAnimals()) return;

  closeModal();
  renderAll();
  showToast(existing ? "Cattle details updated 🌿" : "New cattle registered successfully 🌱");
});

function animalPhotoMarkup(animal, mini = false) {
  if (animal.photo) {
    return `<img src="${animal.photo}" alt="${escapeHTML(animal.name)}">`;
  }
  return `<div class="${mini ? "mini-placeholder" : "photo-placeholder"}">🐄</div>`;
}

function renderDashboard() {
  const total = animals.length;
  const healthy = animals.filter(a => a.health === "Healthy").length;
  const attention = total - healthy;
  const due = animals.filter(a => {
    const status = vaccineStatus(a.nextVaccineDate);
    return status === "today" || status === "overdue";
  }).length;
  const vaccineCount = animals.filter(a => a.vaccineName || a.lastVaccineDate || a.nextVaccineDate).length;
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

  const upcoming = animals
    .filter(a => a.nextVaccineDate && vaccineStatus(a.nextVaccineDate) !== "none")
    .sort((a, b) => a.nextVaccineDate.localeCompare(b.nextVaccineDate))
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

  const recent = animals.slice(0, 3);
  $("recentCattle").innerHTML = recent.length ? recent.map(animal => `
    <div class="mini-animal">
      ${animalPhotoMarkup(animal, true)}
      <div><strong>${escapeHTML(animal.name)}</strong><small>${escapeHTML(animal.tag)} · ${escapeHTML(animal.breed || "Breed not set")}</small></div>
    </div>`).join("") : `
      <div class="empty-state">🐄<strong>Your herd starts here</strong>
      <span>Register your first animal to see it here.</span></div>`;
}

function renderCattle() {
  const search = $("cattleSearch").value.trim().toLowerCase();
  const filter = $("healthFilter").value;

  const filtered = animals.filter(animal => {
    const matchesSearch = [animal.name, animal.tag, animal.breed]
      .some(value => (value || "").toLowerCase().includes(search));
    const matchesHealth = filter === "all" || animal.health === filter;
    return matchesSearch && matchesHealth;
  });

  $("cattleCount").textContent = `${filtered.length} animal${filtered.length === 1 ? "" : "s"}`;

  $("cattleGrid").innerHTML = filtered.length ? filtered.map(animal => `
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
  return [
    "CATTLECARE ANIMAL PROFILE",
    `Name: ${animal.name}`,
    `Animal ID: ${animal.tag}`,
    `Breed: ${animal.breed || "Not recorded"}`,
    `Health: ${animal.health}`,
    `Date of birth: ${animal.dob || "Not recorded"}`,
    `Last vaccination: ${animal.lastVaccineDate || "Not recorded"}`,
    `Vaccine: ${animal.vaccineName || "Not recorded"}`,
    `Next vaccination: ${animal.nextVaccineDate || "Not scheduled"}`,
    `Doctor notes: ${animal.notes || "None"}`
  ].join("\n");
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
  renderVaccinations();
  renderQrCodes();
}

function showPage(pageName) {
  const pageNames = {
    dashboard: "Overview",
    cattle: "My Cattle",
    vaccinations: "Vaccinations",
    qr: "QR Station"
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

function deleteAnimal(id) {
  const animal = animals.find(a => a.id === id);
  if (!animal) return;

  if (!confirm(`Delete ${animal.name} (${animal.tag})? This cannot be undone.`)) return;

  animals = animals.filter(a => a.id !== id);
  if (saveAnimals()) {
    renderAll();
    showToast("Animal record deleted.");
  }
}

// Navigation
document.querySelectorAll(".nav-link").forEach(button => {
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
  if (event.key === "Escape") closeModal();
});

// Cattle search and filtering
$("cattleSearch").addEventListener("input", renderCattle);
$("healthFilter").addEventListener("change", renderCattle);

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

// Mobile sidebar
$("menuToggle").addEventListener("click", () => {
  $("sidebar").classList.toggle("open");
});

// Initial setup
$("todayDate").textContent = new Date().toLocaleDateString(undefined, {
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric"
});
$("footerYear").textContent = new Date().getFullYear();

renderAll();

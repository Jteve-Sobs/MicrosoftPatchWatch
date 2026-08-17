// Per-browser "hidden products" list, kept in localStorage. New products are
// visible by default since we only ever store the *hidden* keys — anything
// not in this set is shown automatically, including versions that show up
// after a later refresh.
const HIDDEN_STORAGE_KEY = "patchwatch_hidden_products";

function getHiddenProducts() {
  try {
    return new Set(JSON.parse(localStorage.getItem(HIDDEN_STORAGE_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function setHiddenProducts(hidden) {
  localStorage.setItem(HIDDEN_STORAGE_KEY, JSON.stringify(Array.from(hidden)));
}

function patchwatchHideProduct(key) {
  const hidden = getHiddenProducts();
  hidden.add(key);
  setHiddenProducts(hidden);
  patchwatchRefreshVisibility();
}

function patchwatchShowProduct(key) {
  const hidden = getHiddenProducts();
  hidden.delete(key);
  setHiddenProducts(hidden);
  patchwatchRefreshVisibility();
}

function patchwatchFilter() {
  patchwatchRefreshVisibility();
}

// Single source of truth for "is this product row visible right now" —
// combines the free-text filter with the user's hidden-products list, so
// hiding a product always wins even if it matches the current filter text.
function patchwatchRefreshVisibility() {
  const hidden = getHiddenProducts();
  const filterInput = document.querySelector(".filter");
  const q = (filterInput ? filterInput.value : "").trim().toLowerCase();

  document.querySelectorAll(".product-row").forEach((row) => {
    const key = row.dataset.key;
    const hiddenByUser = hidden.has(key);
    const matchesFilter = !q || (row.dataset.search || "").includes(q);
    const visible = !hiddenByUser && matchesFilter;
    row.style.display = visible ? "" : "none";

    const historyRow = row.nextElementSibling;
    if (historyRow && historyRow.classList.contains("history-row") && !visible) {
      historyRow.hidden = true; // collapse so it can't linger open behind a hidden row
    }
  });

  document.querySelectorAll(".family-section").forEach((section) => {
    const rows = section.querySelectorAll(".product-row");
    const anyVisible = Array.from(rows).some((row) => row.style.display !== "none");
    section.style.display = rows.length && !anyVisible ? "none" : "";
  });

  renderHiddenPanel(hidden);
}

function renderHiddenPanel(hidden) {
  const i18n = window.PATCHWATCH_I18N || { hiddenNone: "No products hidden.", show: "Show" };

  const countEl = document.getElementById("hidden-count");
  if (countEl) countEl.textContent = String(hidden.size);

  const panel = document.getElementById("hidden-panel");
  if (!panel) return;
  panel.replaceChildren();

  if (hidden.size === 0) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = i18n.hiddenNone;
    panel.appendChild(p);
    return;
  }

  const list = document.createElement("ul");
  list.className = "hidden-list";
  document.querySelectorAll(".product-row").forEach((row) => {
    const key = row.dataset.key;
    if (!hidden.has(key)) return;
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.textContent = row.dataset.name || key;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "link-btn";
    btn.textContent = i18n.show;
    btn.addEventListener("click", () => patchwatchShowProduct(key));
    li.append(span, btn);
    list.appendChild(li);
  });
  panel.appendChild(list);
}

function patchwatchToggleHiddenPanel() {
  const panel = document.getElementById("hidden-panel");
  if (!panel) return;
  panel.hidden = !panel.hidden;
}

function patchwatchToggleHistory(button) {
  const historyRow = button.closest("tr").nextElementSibling;
  if (!historyRow || !historyRow.classList.contains("history-row")) return;
  historyRow.hidden = !historyRow.hidden;
  button.setAttribute("aria-expanded", String(!historyRow.hidden));
  button.classList.toggle("is-open", !historyRow.hidden);
}

// The product table is swapped wholesale via HTMX after a background refresh
// (see index.html #tables); re-apply hidden/filter state to the fresh rows.
document.body.addEventListener("htmx:afterSwap", (evt) => {
  if (evt.target && evt.target.id === "tables") {
    patchwatchRefreshVisibility();
  }
});
document.addEventListener("DOMContentLoaded", patchwatchRefreshVisibility);

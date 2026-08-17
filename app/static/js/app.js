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

// Close the "hidden products" popup on any click outside it (or its toggle
// button), like a normal dropdown/menu.
document.addEventListener("click", (evt) => {
  const panel = document.getElementById("hidden-panel");
  if (!panel || panel.hidden) return;
  if (evt.target.closest("#hidden-panel, .hidden-panel-wrap > .btn")) return;
  panel.hidden = true;
});

// Which products currently have their "Verlauf" panel open. #tables gets
// replaced wholesale by HTMX whenever a background refresh finishes (see
// htmx:afterSwap below), which would otherwise silently re-collapse every
// open history panel — this set is what lets us reopen them afterwards.
const patchwatchOpenHistory = new Set();

function patchwatchToggleHistory(button) {
  const row = button.closest("tr");
  const historyRow = row.nextElementSibling;
  if (!historyRow || !historyRow.classList.contains("history-row")) return;
  historyRow.hidden = !historyRow.hidden;
  button.setAttribute("aria-expanded", String(!historyRow.hidden));
  button.classList.toggle("is-open", !historyRow.hidden);
  const key = row.dataset.key;
  if (historyRow.hidden) {
    patchwatchOpenHistory.delete(key);
  } else {
    patchwatchOpenHistory.add(key);
  }
}

// Re-opens (and re-fetches, since the swapped-in row starts collapsed with
// no content loaded) every history panel that was open before the table got
// replaced.
//
// This used to simulate a click on the history button, relying on HTMX
// having already wired up its hx-trigger listener on the freshly-swapped
// button by the time htmx:afterSwap runs. That's timing-dependent and
// unreliable in practice: the row reopens but htmx's listener isn't there
// yet, so the fetch never fires and the panel stays stuck on the "…"
// placeholder. Calling htmx.ajax() directly sidesteps that entirely — it's
// not waiting on any listener, just performs the same GET/target/swap the
// button's hx-get would have.
function patchwatchReopenHistory() {
  if (patchwatchOpenHistory.size === 0) return;
  document.querySelectorAll(".product-row").forEach((row) => {
    const key = row.dataset.key;
    if (!patchwatchOpenHistory.has(key)) return;
    const historyRow = row.nextElementSibling;
    const button = row.querySelector(".history-btn");
    const body = historyRow?.querySelector(".history-body");
    if (!historyRow || !button || !body) return;

    historyRow.hidden = false;
    button.setAttribute("aria-expanded", "true");
    button.classList.add("is-open");
    htmx.ajax("GET", `/product/${encodeURIComponent(key)}/history`, { target: body, swap: "innerHTML" });
  });
}

// --- Sortable table columns ---------------------------------------------
// Natural compare: numeric chunks compare as numbers, so "10" sorts after
// "6" instead of before it (string sort would put ".NET 10.0" ahead of
// ".NET 6.0"). Mirrors the natural sort used server-side for the default
// row order, see _natural_sort_key in app/routers/web.py.
function patchwatchNaturalCompare(a, b) {
  const chunks = /(\d+)/;
  const partsA = a.split(chunks);
  const partsB = b.split(chunks);
  const len = Math.max(partsA.length, partsB.length);
  for (let i = 0; i < len; i++) {
    const partA = partsA[i] || "";
    const partB = partsB[i] || "";
    if (partA === partB) continue;
    const numA = /^\d+$/.test(partA) ? Number(partA) : null;
    const numB = /^\d+$/.test(partB) ? Number(partB) : null;
    if (numA !== null && numB !== null) {
      if (numA !== numB) return numA - numB;
    } else {
      const cmp = partA.toLowerCase().localeCompare(partB.toLowerCase());
      if (cmp !== 0) return cmp;
    }
  }
  return 0;
}

// Remembers the active sort per product family (table), keyed by
// data-family on the enclosing <section>. Needed because the tables are
// swapped wholesale via HTMX after a background refresh (see below) — a
// plain in-DOM sort would otherwise silently reset on every refresh.
const patchwatchSortState = {};

function patchwatchApplySort(table, col, dir) {
  const headers = Array.from(table.querySelectorAll("thead th"));
  headers.forEach((th) => {
    if (!th.classList.contains("sortable")) return;
    th.setAttribute("aria-sort", th.dataset.sortCol === col ? (dir === "asc" ? "ascending" : "descending") : "none");
  });
  const colIndex = headers.findIndex((th) => th.dataset.sortCol === col);
  if (colIndex === -1) return;

  const tbody = table.querySelector("tbody");
  const rows = Array.from(tbody.children);
  const pairs = [];
  for (let i = 0; i < rows.length; i++) {
    if (!rows[i].classList.contains("product-row")) continue;
    const historyRow = rows[i + 1] && rows[i + 1].classList.contains("history-row") ? rows[i + 1] : null;
    pairs.push({ row: rows[i], historyRow });
  }

  const mult = dir === "asc" ? 1 : -1;
  pairs.sort((a, b) => {
    const cellA = a.row.children[colIndex];
    const cellB = b.row.children[colIndex];
    const valA = (cellA && cellA.dataset.sort) || "";
    const valB = (cellB && cellB.dataset.sort) || "";
    // Missing values ("–") always sort to the bottom, in either direction.
    if (!valA && !valB) return 0;
    if (!valA) return 1;
    if (!valB) return -1;
    return patchwatchNaturalCompare(valA, valB) * mult;
  });

  const frag = document.createDocumentFragment();
  pairs.forEach(({ row, historyRow }) => {
    frag.appendChild(row);
    if (historyRow) frag.appendChild(historyRow);
  });
  tbody.appendChild(frag);
}

function patchwatchApplyStoredSort(table) {
  const family = table.closest(".family-section")?.dataset.family;
  const state = family ? patchwatchSortState[family] : undefined;
  if (state) patchwatchApplySort(table, state.col, state.dir);
}

function patchwatchSortTableBy(th) {
  const table = th.closest("table");
  const family = table && table.closest(".family-section")?.dataset.family;
  if (!table || !family) return;
  const col = th.dataset.sortCol;
  const current = patchwatchSortState[family];
  const dir = current && current.col === col && current.dir === "asc" ? "desc" : "asc";
  patchwatchSortState[family] = { col, dir };
  patchwatchApplySort(table, col, dir);
}

document.addEventListener("click", (evt) => {
  const th = evt.target.closest("th.sortable");
  if (th) patchwatchSortTableBy(th);
});
document.addEventListener("keydown", (evt) => {
  if (evt.key !== "Enter" && evt.key !== " ") return;
  const th = evt.target.closest("th.sortable");
  if (!th) return;
  evt.preventDefault();
  patchwatchSortTableBy(th);
});

// The product table is swapped wholesale via HTMX after a background refresh
// (see index.html #tables); re-apply hidden/filter state and any active
// column sort to the fresh rows.
document.body.addEventListener("htmx:afterSwap", (evt) => {
  if (evt.target && evt.target.id === "tables") {
    patchwatchRefreshVisibility();
    evt.target.querySelectorAll(".patch-table").forEach(patchwatchApplyStoredSort);
    patchwatchReopenHistory();
  }
});
document.addEventListener("DOMContentLoaded", () => {
  patchwatchRefreshVisibility();
  document.querySelectorAll(".patch-table").forEach(patchwatchApplyStoredSort);
});

// --- "Jetzt aktualisieren" feedback --------------------------------------
// /refresh is debounced server-side (MIN_REFRESH_INTERVAL_MINUTES) and the
// button uses hx-swap="none" (there's nothing to swap into), so a debounced
// click previously produced literally zero visible feedback — indistinguish-
// able from the button being broken. This surfaces what actually happened.
let patchwatchRefreshFeedbackTimer;
document.body.addEventListener("htmx:afterRequest", (evt) => {
  if (evt.detail.elt?.id !== "refresh-btn") return;
  const feedback = document.getElementById("refresh-feedback");
  if (!feedback) return;

  let data = {};
  try {
    data = JSON.parse(evt.detail.xhr.responseText);
  } catch {
    // Non-JSON/error response: nothing useful to show, fall through to clear.
  }

  const i18n = window.PATCHWATCH_I18N || {};
  if (data.started) {
    feedback.textContent = "";
    // Don't make the user wait up to 20s for the next status-bar poll to
    // notice the check that just started.
    htmx.ajax("GET", "/partials/status", { target: "#status-bar", swap: "innerHTML" });
  } else if (data.running) {
    feedback.textContent = i18n.refreshRunning || "";
  } else {
    feedback.textContent = i18n.refreshDebounced || "";
  }

  clearTimeout(patchwatchRefreshFeedbackTimer);
  if (feedback.textContent) {
    patchwatchRefreshFeedbackTimer = setTimeout(() => { feedback.textContent = ""; }, 6000);
  }
});

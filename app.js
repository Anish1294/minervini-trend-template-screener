/**
 * VCP Scanner — Minervini Trend Template Institutional Terminal
 * High-performance client-side data engine & interactive 8-rule diagnostic
 */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

// Application State
const state = {
  config: null,
  stocks: [],
  filtered: [],
  sortKey: "rs_rating",
  sortDirection: "desc",
  activeChip: "all",
  starred: new Set(),
  selectedTicker: null,
  isUsingFallback: false
};

// Utilities & Formatters
const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
const escapeHtml = (str) =>
  String(str ?? "").replace(/[&<>'"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));

const ratioPercent = (val) => (num(val) === null ? null : (num(val) - 1) * 100);

const highDistance = (stock) => {
  if (num(stock.below_52w_high_pct) !== null) return num(stock.below_52w_high_pct);
  return num(stock.price_to_52w_high) === null ? null : (1 - num(stock.price_to_52w_high)) * 100;
};

const signed = (val) => (val === null ? "—" : `${val >= 0 ? "+" : ""}${val.toFixed(1)}%`);
const percent = (val) => (val === null ? "—" : `${val.toFixed(1)}%`);
const money = (val) => (val === null ? "—" : `$${val.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

const median = (values) => {
  const valid = values.filter((v) => v !== null).sort((a, b) => a - b);
  if (!valid.length) return null;
  const mid = Math.floor(valid.length / 2);
  return valid.length % 2 ? valid[mid] : (valid[mid - 1] + valid[mid]) / 2;
};

// Deterministic pastel color matching Stage 6 StockLogo.tsx
function tickerColor(ticker) {
  let hash = 0;
  for (let i = 0; i < ticker.length; i++) {
    hash = ticker.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 55%, 42%)`;
}

// Watchlist Persistence
function initWatchlist() {
  try {
    const raw = localStorage.getItem("vcp_minervini_watchlist");
    if (raw) {
      state.starred = new Set(JSON.parse(raw));
    }
  } catch (e) {
    state.starred = new Set();
  }
  updateWatchlistCount();
}

function toggleStar(ticker) {
  if (state.starred.has(ticker)) {
    state.starred.delete(ticker);
  } else {
    state.starred.add(ticker);
  }
  try {
    localStorage.setItem("vcp_minervini_watchlist", JSON.stringify([...state.starred]));
  } catch (e) {}
  
  updateWatchlistCount();
  applyFilters();
  
  // If drawer is currently open for this ticker, update its star button
  if (state.selectedTicker === ticker) {
    const starBtn = $("#detail-star-btn");
    if (starBtn) {
      starBtn.classList.toggle("starred", state.starred.has(ticker));
    }
  }
}

function updateWatchlistCount() {
  const countEl = $("#watchlist-count");
  if (countEl) countEl.textContent = state.starred.size;
}

// Load Application Data
async function load() {
  initWatchlist();
  
  try {
    // 1. Load Strategy Config
    state.config = await fetch("strategy.json").then((r) => {
      if (!r.ok) throw new Error("Could not load strategy.json");
      return r.json();
    });
    bindProductLinks();

    // 2. Attempt to load snapshot from remote R2 endpoint
    let payload = null;
    try {
      payload = await fetch(state.config.data.url, { cache: "default" }).then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      });
    } catch (fetchErr) {
      console.warn("Remote R2 snapshot not yet populated, loading verified baseline snapshot", fetchErr);
      state.isUsingFallback = true;
      payload = await fetch("sample-snapshot.json").then((r) => {
        if (!r.ok) throw new Error("Could not load sample-snapshot.json");
        return r.json();
      });
    }

    if (!payload || !Array.isArray(payload.stocks)) {
      throw new Error("Invalid payload: missing stocks array");
    }

    state.stocks = payload.stocks.map(normalizeStock).filter((s) => s.ticker);
    
    if (state.isUsingFallback) {
      $("#fallback-notice")?.classList.remove("hidden");
    }

    renderFreshness(payload);
    renderStats();
    applyFilters();

    $("#loading-state").classList.add("hidden");
    $("#table-state").classList.remove("hidden");

  } catch (err) {
    console.error("Critical failure during initialization", err);
    $("#loading-state").classList.add("hidden");
    const emptyState = $("#empty-state");
    if (emptyState) {
      emptyState.classList.remove("hidden");
      emptyState.querySelector("h3").textContent = "Unable to connect to market snapshot";
      emptyState.querySelector("p").textContent = "VCPScanner data snapshot is currently syncing. Please try again shortly or open VCPScanner directly.";
    }
    $("#table-state").classList.remove("hidden");
  }
}

function normalizeStock(stock) {
  const price = num(stock.price);
  const sma50 = num(stock.price_to_sma50);
  const sma200 = num(stock.price_to_sma200);
  const highDist = highDistance(stock);

  return {
    ...stock,
    ticker: String(stock.ticker || "").toUpperCase(),
    name: stock.name || stock.company_name || stock.ticker || "",
    price,
    change_pct: num(stock.change_pct),
    market_cap: num(stock.market_cap),
    rs_rating: num(stock.rs_rating),
    price_to_sma50: sma50,
    price_to_sma200: sma200,
    price_to_52w_high: num(stock.price_to_52w_high),
    below_52w_high_pct: highDist,
    volume_ratio: num(stock.volume_ratio)
  };
}

function bindProductLinks() {
  const terminal = state.config?.terminal || state.config?.conversion || {
    label: "Open Full Terminal",
    url: "https://vcpscanner.com/screens/minervini-trend-template?utm_source=github&utm_medium=screen_app&utm_campaign=minervini-trend-template-screener"
  };
  $$("[data-product-link]").forEach((link) => {
    link.href = terminal.url;
  });
}

function renderFreshness(payload) {
  const stamp = payload.data_as_of || payload.generated_at;
  const dateFormatted = stamp
    ? new Date(stamp).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "Market Close Verified";
    
  $("#as-of").textContent = `Verified: ${dateFormatted}`;
  $("#universe-note").textContent = `${state.stocks.length} Qualified US Common Equities ($500M+ Cap)`;
}

function renderStats() {
  const count = state.stocks.length;
  $("#stat-count").textContent = count.toLocaleString();
  
  // Universe estimate (approx 5,800 liquid US common stocks)
  const pct = Math.max(1, Math.min(100, Math.round((count / 5800) * 100)));
  const qualBar = $("#kpi-qual-bar");
  if (qualBar) qualBar.style.width = `${pct}%`;
  
  const pctEl = $("#stat-universe-pct");
  if (pctEl) pctEl.textContent = `${((count / 5800) * 100).toFixed(1)}% of US market`;

  const medianRs = median(state.stocks.map((s) => s.rs_rating));
  const medianHigh = median(state.stocks.map((s) => s.below_52w_high_pct));
  const nearHighCount = state.stocks.filter((s) => s.below_52w_high_pct !== null && s.below_52w_high_pct <= 5.0).length;

  $("#stat-rs").textContent = medianRs === null ? "—" : Math.round(medianRs);
  $("#stat-high").textContent = medianHigh === null ? "—" : `-${medianHigh.toFixed(1)}%`;
  const high5El = $("#stat-high5");
  if (high5El) high5El.textContent = `${nearHighCount} Setups`;
}

// Filter Logic
function applyFilters() {
  const query = $("#search").value.trim().toLowerCase();
  const minimumRs = Number($("#rs-filter").value);
  const maximumHighDistance = Number($("#high-filter").value);
  const minimumVol = Number($("#vol-filter")?.value || 0);

  state.filtered = state.stocks.filter((stock) => {
    // 1. Text Search
    const matchesQuery =
      !query ||
      stock.ticker.toLowerCase().includes(query) ||
      stock.name.toLowerCase().includes(query);

    // 2. Select Dropdowns
    const matchesRs = stock.rs_rating !== null && stock.rs_rating >= minimumRs;
    const matchesHigh = stock.below_52w_high_pct !== null && stock.below_52w_high_pct <= maximumHighDistance;
    const matchesVol = minimumVol <= 0 || (stock.volume_ratio !== null && stock.volume_ratio >= minimumVol);

    // 3. Quick Chips
    let matchesChip = true;
    if (state.activeChip === "RS90") {
      matchesChip = stock.rs_rating !== null && stock.rs_rating >= 90;
    } else if (state.activeChip === "HIGH5") {
      matchesChip = stock.below_52w_high_pct !== null && stock.below_52w_high_pct <= 5.0;
    } else if (state.activeChip === "VOL") {
      matchesChip = stock.volume_ratio !== null && stock.volume_ratio >= 1.5;
    } else if (state.activeChip === "STARRED") {
      matchesChip = state.starred.has(stock.ticker);
    }

    return matchesQuery && matchesRs && matchesHigh && matchesVol && matchesChip;
  });

  sortRows();
  renderRows();
}

function sortRows() {
  const dir = state.sortDirection === "asc" ? 1 : -1;
  state.filtered.sort((a, b) => {
    let valA = a[state.sortKey];
    let valB = b[state.sortKey];

    if (valA === null || valA === undefined) return 1;
    if (valB === null || valB === undefined) return -1;

    if (typeof valA === "string") {
      return valA.localeCompare(valB) * dir;
    }
    return (valA - valB) * dir;
  });
}

function renderRows() {
  const count = state.filtered.length;
  $("#result-count").textContent = `${count.toLocaleString()} candidate${count === 1 ? "" : "s"}`;

  const tbody = $("#stock-rows");
  const emptyState = $("#empty-state");

  if (count === 0) {
    tbody.innerHTML = "";
    emptyState.classList.remove("hidden");
    return;
  }

  emptyState.classList.add("hidden");

  tbody.innerHTML = state.filtered.map((stock) => {
    const isStarred = state.starred.has(stock.ticker);
    const dayGain = stock.change_pct || 0;
    const isPos = dayGain >= 0;
    const changeClass = isPos ? "pos" : "neg";
    const rs = stock.rs_rating === null ? "—" : Math.round(stock.rs_rating);
    const rsClass = stock.rs_rating >= 95 ? "rs-super" : stock.rs_rating >= 90 ? "rs-elite" : "";

    // 52W High progress indicator: if 0% below high => 100% full bar
    const gap = stock.below_52w_high_pct ?? 25;
    const rangePct = Math.max(10, Math.min(100, Math.round(100 - (gap / 25) * 60)));

    // Vol Ratio bar
    const vol = stock.volume_ratio || 1.0;
    const volWidth = Math.min(100, Math.round((vol / 2.5) * 100));
    const volClass = vol >= 1.5 ? "high" : "";

    const logoUrl = `https://assets.vcpscanner.com/logos/${stock.ticker}.webp`;
    const fallbackBg = tickerColor(stock.ticker);
    const initial = stock.ticker.charAt(0);

    const isSelected = state.selectedTicker === stock.ticker ? "active-row" : "";

    return `
      <tr data-ticker="${escapeHtml(stock.ticker)}" class="${isSelected}">
        <td class="td-star" onclick="event.stopPropagation(); window.toggleStar('${escapeHtml(stock.ticker)}')">
          <button class="star-btn ${isStarred ? "starred" : ""}" type="button" title="Star Stock">★</button>
        </td>
        <td>
          <div class="stock-cell">
            <img class="stock-logo" src="${logoUrl}" alt="${escapeHtml(stock.ticker)}" loading="lazy"
                 onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
            <span class="stock-logo-fallback" style="display:none; background-color: ${fallbackBg};">${initial}</span>
            <div class="stock-info">
              <span class="stock-ticker">${escapeHtml(stock.ticker)}</span>
              <span class="stock-name" title="${escapeHtml(stock.name)}">${escapeHtml(stock.name)}</span>
            </div>
          </div>
        </td>
        <td class="td-num">${money(stock.price)}</td>
        <td class="td-num">
          <span class="change-pill ${changeClass}">${signed(stock.change_pct)}</span>
        </td>
        <td class="td-num">
          <strong class="rs-val ${rsClass}">${rs}</strong>
        </td>
        <td class="td-num text-teal">${signed(ratioPercent(stock.price_to_sma50))}</td>
        <td class="td-num text-teal">${signed(ratioPercent(stock.price_to_sma200))}</td>
        <td class="range-cell">
          <div class="range-wrap">
            <div class="range-track">
              <div class="range-fill" style="width: ${rangePct}%;"></div>
            </div>
            <span class="range-label">-${percent(stock.below_52w_high_pct)}</span>
          </div>
        </td>
        <td class="vol-cell td-num">
          <div class="vol-wrap">
            <span class="font-mono">${vol.toFixed(2)}×</span>
            <div class="vol-bar">
              <div class="vol-fill ${volClass}" style="width: ${volWidth}%;"></div>
            </div>
          </div>
        </td>
        <td class="th-actions" onclick="event.stopPropagation();">
          <a class="row-chart-link" href="https://vcpscanner.com/technical/${stock.ticker.toLowerCase()}?utm_source=github&utm_medium=table_row&utm_campaign=minervini-trend-template-screener" target="_blank" rel="noopener noreferrer" title="Open chart on VCPScanner">
            Chart ↗
          </a>
        </td>
      </tr>
    `;
  }).join("");

  updateSortHeaders();
}

function updateSortHeaders() {
  $$("th[data-sort]").forEach((th) => {
    const key = th.dataset.sort;
    const arrow = th.querySelector(".sort-arrow");
    if (state.sortKey === key) {
      arrow.textContent = state.sortDirection === "asc" ? "▲" : "▼";
      th.classList.add("sorted");
    } else {
      arrow.textContent = "";
      th.classList.remove("sorted");
    }
  });
}

// Two-Tier Minervini Diagnostic Drawer
function showDetail(stock) {
  if (!stock) return;
  state.selectedTicker = stock.ticker;

  // Header information
  $("#detail-ticker").textContent = stock.ticker;
  $("#detail-name").textContent = stock.name;

  const logoWrap = $("#detail-logo-wrap");
  const logoUrl = `https://assets.vcpscanner.com/logos/${stock.ticker}.webp`;
  const fallbackBg = tickerColor(stock.ticker);
  logoWrap.innerHTML = `
    <img src="${logoUrl}" alt="${stock.ticker}" onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';">
    <span class="stock-logo-fallback" style="display:none; background-color: ${fallbackBg};">${stock.ticker.charAt(0)}</span>
  `;

  const starBtn = $("#detail-star-btn");
  if (starBtn) {
    starBtn.classList.toggle("starred", state.starred.has(stock.ticker));
  }

  const badge = $("#detail-badge");
  if (badge) {
    badge.className = "badge ready";
    badge.textContent = "STAGE 2 CANDIDATE";
  }

  // Stats strip
  $("#detail-price").textContent = money(stock.price);
  const changeEl = $("#detail-change");
  changeEl.textContent = signed(stock.change_pct);
  changeEl.className = `d-stat-val font-mono ${(stock.change_pct || 0) >= 0 ? "text-teal" : "text-red"}`;

  $("#detail-mcap").textContent = stock.market_cap ? `$${(stock.market_cap / 1e9).toFixed(1)}B` : "—";
  $("#detail-rs").textContent = stock.rs_rating ? `${Math.round(stock.rs_rating)} / 99` : "—";

  // Tier 1: Real Calculated Pre-Screen Evidence
  const sma50Spread = ratioPercent(stock.price_to_sma50);
  const sma200Spread = ratioPercent(stock.price_to_sma200);

  $("#rule-1-val").textContent = sma200Spread !== null ? `${signed(sma200Spread)} vs 200` : "Passed";
  $("#rule-2-val").textContent = sma50Spread !== null ? `${signed(sma50Spread)} vs 50` : "Passed";
  $("#rule-3-val").textContent = stock.below_52w_high_pct !== null ? `-${stock.below_52w_high_pct.toFixed(1)}% from High` : "Within 25%";
  $("#rule-4-val").textContent = stock.rs_rating ? `${Math.round(stock.rs_rating)} / 99` : "RS ≥ 70";

  // Direct Chart Links
  const chartUrl = `https://vcpscanner.com/technical/${stock.ticker.toLowerCase()}?utm_source=github&utm_medium=inspector_audit&utm_campaign=minervini-trend-template-screener`;
  $("#detail-link").href = chartUrl;
  const link5 = $("#rule-5-link");
  if (link5) link5.href = chartUrl;
  const link6 = $("#rule-6-link");
  if (link6) link6.href = chartUrl;

  // Display Drawer & Overlay
  $("#inspector-overlay").classList.remove("hidden");
  $("#detail").classList.remove("hidden");
  document.body.style.overflow = "hidden"; // Prevent body scroll while inspecting

  // Highlight row in table
  $$("#stock-rows tr").forEach((row) => {
    row.classList.toggle("active-row", row.dataset.ticker === stock.ticker);
  });
}

function closeDetail() {
  $("#inspector-overlay").classList.add("hidden");
  $("#detail").classList.add("hidden");
  document.body.style.overflow = "";
  state.selectedTicker = null;
  $$("#stock-rows tr").forEach((row) => row.classList.remove("active-row"));
}

// CSV Export
function exportCsv() {
  if (!state.filtered.length) return;

  const headers = [
    "Ticker",
    "Company Name",
    "Price",
    "Day Change %",
    "RS Rating",
    "Price vs 50 SMA %",
    "Price vs 200 SMA %",
    "Distance from 52W High %",
    "Volume Ratio",
    "Market Cap"
  ];

  const rows = state.filtered.map((stock) => [
    stock.ticker,
    stock.name,
    stock.price ?? "",
    stock.change_pct ?? "",
    stock.rs_rating ?? "",
    ratioPercent(stock.price_to_sma50)?.toFixed(2) ?? "",
    ratioPercent(stock.price_to_sma200)?.toFixed(2) ?? "",
    stock.below_52w_high_pct?.toFixed(2) ?? "",
    stock.volume_ratio ?? "",
    stock.market_cap ?? ""
  ]);

  const csvContent = [
    headers.join(","),
    ...rows.map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(","))
  ].join("\n");

  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const dateStr = new Date().toISOString().slice(0, 10);
  link.href = url;
  link.download = `minervini-trend-template-stocks-${dateStr}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

// Event Listeners
function setupEvents() {
  // Search Input
  const searchInput = $("#search");
  const searchClear = $("#search-clear");

  searchInput.addEventListener("input", () => {
    if (searchInput.value.trim().length > 0) {
      searchClear.classList.remove("hidden");
    } else {
      searchClear.classList.add("hidden");
    }
    applyFilters();
  });

  searchClear.addEventListener("click", () => {
    searchInput.value = "";
    searchClear.classList.add("hidden");
    searchInput.focus();
    applyFilters();
  });

  // Dropdown Filters
  $("#rs-filter").addEventListener("change", applyFilters);
  $("#high-filter").addEventListener("change", applyFilters);
  $("#vol-filter")?.addEventListener("change", applyFilters);

  // Quick Filter Chips
  $$(".filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      $$(".filter-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      state.activeChip = chip.dataset.chip;
      applyFilters();
    });
  });

  // Watchlist Toggle in Header
  $("#watchlist-toggle-btn")?.addEventListener("click", () => {
    $$(".filter-chip").forEach((c) => c.classList.remove("active"));
    const starChip = $('[data-chip="STARRED"]');
    if (starChip) starChip.classList.add("active");
    state.activeChip = "STARRED";
    applyFilters();
  });

  // Table Sorting
  $$("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (state.sortKey === key) {
        state.sortDirection = state.sortDirection === "desc" ? "asc" : "desc";
      } else {
        state.sortKey = key;
        state.sortDirection = ["ticker", "name"].includes(key) ? "asc" : "desc";
      }
      applyFilters();
    });
  });

  // Table Row Click -> Open Inspector
  $("#stock-rows").addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-ticker]");
    if (!row) return;
    const ticker = row.dataset.ticker;
    const stock = state.stocks.find((s) => s.ticker === ticker);
    if (stock) showDetail(stock);
  });

  // Close Inspector
  $("#detail-close").addEventListener("click", closeDetail);
  $("#inspector-overlay").addEventListener("click", closeDetail);

  // Detail Star Toggle
  $("#detail-star-btn")?.addEventListener("click", () => {
    if (state.selectedTicker) toggleStar(state.selectedTicker);
  });

  // CSV Export
  $("#export").addEventListener("click", exportCsv);

  // Reset Filters Button
  $("#reset-filters-btn")?.addEventListener("click", () => {
    $("#search").value = "";
    $("#rs-filter").value = "70";
    $("#high-filter").value = "25";
    if ($("#vol-filter")) $("#vol-filter").value = "0";
    $$(".filter-chip").forEach((c) => c.classList.remove("active"));
    $('[data-chip="all"]').classList.add("active");
    state.activeChip = "all";
    applyFilters();
  });

  // Keyboard Shortcuts
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && document.activeElement !== searchInput) {
      e.preventDefault();
      searchInput.focus();
    } else if (e.key === "Escape") {
      if (!$("#detail").classList.contains("hidden")) {
        closeDetail();
      } else if (document.activeElement === searchInput) {
        searchInput.blur();
      }
    }
  });

  // Expose toggleStar globally for inline onclick
  window.toggleStar = toggleStar;
}

// Initialize
setupEvents();
load();

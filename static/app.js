const state = { signals: [], filter: "all", query: "", nextScanAt: null };

const $ = (selector) => document.querySelector(selector);
const fmtPrice = (value) => {
  if (value == null) return "—";
  const digits = value >= 1000 ? 2 : value >= 1 ? 4 : 8;
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: digits });
};
const fmtTime = (iso) => new Intl.DateTimeFormat("he-IL", {
  dateStyle: "short", timeStyle: "medium"
}).format(new Date(iso));
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
}[char]));

function renderSignals() {
  const visible = state.signals.filter((signal) => {
    const filterMatch = state.filter === "all" || signal.relevance === state.filter;
    const queryMatch = signal.symbol.toLowerCase().includes(state.query.toLowerCase());
    return filterMatch && queryMatch;
  });
  if (!visible.length) {
    $("#signalList").innerHTML = '<div class="empty">אין איתותים התואמים לסינון.</div>';
    return;
  }
  $("#signalList").innerHTML = visible.map((signal) => {
    const actionable = signal.relevance === "ACTIONABLE";
    const tfCards = Object.entries(signal.timeframes).map(([tf, view]) => `
      <div class="tf-card">
        <strong>${tf}</strong>
        <span>${escapeHtml(view.trend)}</span>
        <span>${escapeHtml(view.structure)} · RSI ${view.rsi}</span>
        <span>Bias ${view.bias} · ATR ${view.atr_pct}%</span>
      </div>`).join("");
    const reasonItems = signal.reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
    return `
      <article class="signal ${signal.direction.toLowerCase()} ${actionable ? "actionable" : ""}">
        <div class="signal-main">
          <div class="symbol">${escapeHtml(signal.symbol)}
            <small>${fmtTime(signal.created_at)} · ${escapeHtml(signal.status)}</small>
          </div>
          <div>
            <div class="direction">
              <span class="badge ${actionable ? signal.direction.toLowerCase() : "neutral"}">
                ${actionable ? signal.direction : "לא רלוונטי כרגע"}
              </span>
              <strong>${signal.confidence}%</strong>
            </div>
            <div class="confidence-track"><i style="width:${signal.confidence}%"></i></div>
          </div>
          <div class="price-grid">
            <span>PRICE <strong>${fmtPrice(signal.price)}</strong></span>
            <span>SL <strong>${fmtPrice(signal.stop_loss)}</strong></span>
            <span>TP1 <strong>${fmtPrice(signal.take_profit_1)}</strong></span>
            <span>TP2 <strong>${fmtPrice(signal.take_profit_2)}</strong></span>
          </div>
          <span class="chevron">⌄</span>
        </div>
        <div class="details">
          <div><h3>נימוקי המנוע</h3><ul class="reason-list">${reasonItems}</ul></div>
          <div><h3>תמונה רב־טיימפריימית</h3><div class="tf-grid">${tfCards}</div></div>
        </div>
      </article>`;
  }).join("");
  document.querySelectorAll(".signal-main").forEach((row) => {
    row.addEventListener("click", () => row.parentElement.classList.toggle("open"));
  });
}

async function loadSignals() {
  const response = await fetch("/api/signals", { cache: "no-store" });
  if (!response.ok) throw new Error("signal request failed");
  const payload = await response.json();
  state.signals = payload.signals;
  if (state.signals.length) $("#lastUpdate").textContent = `עדכון אחרון: ${fmtTime(state.signals[0].created_at)}`;
  renderSignals();
}

async function loadStats() {
  const response = await fetch("/api/stats", { cache: "no-store" });
  const stats = await response.json();
  $("#totalSignals").textContent = stats.total;
  $("#actionableSignals").textContent = stats.actionable;
  $("#resolvedSignals").textContent = stats.resolved;
  $("#winRate").textContent = stats.win_rate == null ? "—" : `${stats.win_rate.toFixed(1)}%`;
}

async function loadStatus() {
  const response = await fetch("/api/status", { cache: "no-store" });
  const status = await response.json();
  state.nextScanAt = new Date(status.next_scan_at);
  const dot = $("#statusDot");
  dot.className = `status-dot ${status.last_error ? "error" : "live"}`;
  $("#statusText").textContent = status.last_error
    ? `שגיאה: ${status.last_error}`
    : status.scanning ? "סורק את השוק..." : "המערכת פעילה";
  $("#scanButton").disabled = status.scanning;
}

function updateCountdown() {
  if (!state.nextScanAt) return;
  const seconds = Math.max(0, Math.floor((state.nextScanAt - new Date()) / 1000));
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  const rest = (seconds % 60).toString().padStart(2, "0");
  $("#countdown").textContent = `סריקה הבאה בעוד ${minutes}:${rest}`;
}

async function refresh() {
  try {
    await Promise.all([loadStatus(), loadSignals(), loadStats()]);
  } catch (error) {
    $("#statusDot").className = "status-dot error";
    $("#statusText").textContent = "החיבור לשרת המקומי נותק";
  }
}

document.querySelectorAll(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    renderSignals();
  });
});
$("#symbolSearch").addEventListener("input", (event) => {
  state.query = event.target.value.trim();
  renderSignals();
});
$("#scanButton").addEventListener("click", async () => {
  $("#scanButton").disabled = true;
  await fetch("/api/scan", { method: "POST" });
  setTimeout(refresh, 1000);
});

refresh();
setInterval(refresh, 10000);
setInterval(updateCountdown, 1000);

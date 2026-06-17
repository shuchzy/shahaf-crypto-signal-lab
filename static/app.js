const state = {
  signals: [],
  filter: "all",
  query: "",
  nextScanAt: null,
  live: false,
  reconnectTimer: null,
  eventSource: null,
};

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
    const searchValue = `${signal.symbol} ${signal.exchange || ""}`.toLowerCase();
    return filterMatch && searchValue.includes(state.query.toLowerCase());
  });
  if (!visible.length) {
    $("#signalList").innerHTML = '<div class="empty">אין איתותים התואמים לסינון.</div>';
    return;
  }
  $("#signalList").innerHTML = visible.map((signal) => {
    const actionable = signal.relevance === "ACTIONABLE";
    const exchange = signal.exchange || "Binance";
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
            <small>
              <span class="exchange ${exchange.toLowerCase()}">${escapeHtml(exchange)}</span>
              ${fmtTime(signal.created_at)} · ${escapeHtml(signal.status)}
            </small>
          </div>
          <div>
            <div class="direction">
              <span class="badge ${actionable ? signal.direction.toLowerCase() : "neutral"}">
                ${actionable ? signal.direction : "לא רלוונטי כרגע"}
              </span>
              <strong>${signal.confidence}%</strong>
            </div>
            <small>
              ${escapeHtml(signal.setup_type || "setup")} · איכות ${signal.setup_quality || 0}/12 · יעד ${signal.estimated_win_rate || "—"}%
            </small>
            <div class="confidence-track"><i style="width:${signal.confidence}%"></i></div>
          </div>
          <div class="price-grid">
            <span>PRICE <strong>${fmtPrice(signal.price)}</strong></span>
            <span>SL <strong>${fmtPrice(signal.stop_loss)}</strong></span>
            <span>TP1 <strong>${fmtPrice(signal.take_profit_1)}</strong></span>
            <span>TP2 <strong>${fmtPrice(signal.take_profit_2)}</strong></span>
            <span>R:R <strong>${signal.risk_reward ? `1:${signal.risk_reward}` : "—"}</strong></span>
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

function applySnapshot(payload) {
  const { status, signals, stats } = payload;
  state.signals = signals || [];
  state.nextScanAt = new Date(status.next_scan_at);
  const demo = stats.demo || {};
  $("#totalTrades").textContent = demo.total_trades || 0;
  $("#openTrades").textContent = demo.open_trades || 0;
  $("#winRate").textContent = demo.win_rate == null ? "—" : `${demo.win_rate.toFixed(1)}%`;
  $("#openPnl").textContent = `$${Number(demo.open_pnl || 0).toFixed(2)}`;
  $("#openPnl").className = Number(demo.open_pnl || 0) >= 0 ? "positive" : "negative";
  $("#realizedPnl").textContent = `$${Number(demo.realized_pnl || 0).toFixed(2)}`;
  $("#realizedPnl").className = Number(demo.realized_pnl || 0) >= 0 ? "positive" : "negative";
  $("#totalPnl").textContent = `$${Number(demo.total_pnl || 0).toFixed(2)}`;
  $("#totalPnl").className = Number(demo.total_pnl || 0) >= 0 ? "positive" : "negative";
  if (state.signals.length) {
    $("#lastUpdate").textContent = `עדכון חי אחרון: ${fmtTime(state.signals[0].created_at)}`;
  }
  const connectedMarkets = Object.entries(status.markets || {})
    .filter(([, health]) => health.ok)
    .map(([name]) => name);
  const marketLabel = connectedMarkets.length ? connectedMarkets.join(" + ") : "מקורות השוק";
  $("#statusText").textContent = status.last_error
    ? `שגיאת מקור נתונים: ${status.last_error}`
    : status.scanning ? `LIVE · סורק ${marketLabel}...` : `LIVE · ${marketLabel}`;
  $("#scanButton").disabled = status.scanning;
  renderSignals();
}

async function initialLoad() {
  const [statusResponse, signalsResponse, statsResponse] = await Promise.all([
    fetch("/api/status", { cache: "no-store" }),
    fetch("/api/signals", { cache: "no-store" }),
    fetch("/api/stats", { cache: "no-store" }),
  ]);
  applySnapshot({
    status: await statusResponse.json(),
    signals: (await signalsResponse.json()).signals,
    stats: await statsResponse.json(),
  });
}

function setConnection(connected) {
  state.live = connected;
  $("#statusDot").className = `status-dot ${connected ? "live" : "error"}`;
  $("#connectionMode").textContent = connected ? "LIVE STREAM" : "מתחבר מחדש...";
  if (!connected) $("#statusText").textContent = "מתחבר מחדש לשירות הענן...";
}

function connectLiveStream() {
  if (state.eventSource) state.eventSource.close();
  const source = new EventSource("/api/live");
  state.eventSource = source;
  source.addEventListener("open", () => setConnection(true));
  source.addEventListener("snapshot", (event) => {
    setConnection(true);
    applySnapshot(JSON.parse(event.data));
  });
  source.addEventListener("error", () => {
    setConnection(false);
    source.close();
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(connectLiveStream, 3000);
  });
}

function updateCountdown() {
  if (!state.nextScanAt) return;
  const seconds = Math.max(0, Math.floor((state.nextScanAt - new Date()) / 1000));
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  const rest = (seconds % 60).toString().padStart(2, "0");
  $("#countdown").textContent = `סריקה הבאה בעוד ${minutes}:${rest}`;
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
});

initialLoad().catch(() => setConnection(false));
connectLiveStream();
setInterval(updateCountdown, 1000);

const state = {
  channel: "headquarters",
  roster: [],
  snapshot: { messages: [], agent_status: {}, reports: {}, run: null, activity: {} },
  book: { positions: [], orders: [], settings: {} },
  lab: { messages: [], trials: [], accepted: [], scrapped: [], status: "idle" },
  charts: [{ id: "es-5m", contract: "ES1!", timeframe: "5m" }],
  chartId: "es-5m",
  chartData: null,
  libraryRows: [],
  librarySort: { key: "net_profit", dir: "desc" },
  paint: "",
};

const channelsEl = document.querySelector("#channels");
const feedEl = document.querySelector("#feed");
const floorEl = document.querySelector("#floor");
const titleEl = document.querySelector("#channel-title");
const teamEl = document.querySelector("#channel-team");
const roleEl = document.querySelector("#channel-role");
const pillEl = document.querySelector("#run-pill");
const nowText = document.querySelector("#now-text");
const reportEl = document.querySelector("#report-body");
const stopBtn = document.querySelector("#stop");
const startBtn = document.querySelector("#start");

function money(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "$0.00";
  const sign = number < 0 ? "-" : "";
  return `${sign}$${Math.abs(number).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function escapeHtml(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch]));
}

function tickClock() {
  const now = new Date();
  const time = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(now);
  const date = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    month: "short",
    day: "numeric",
  }).format(now);
  const clock = document.querySelector("#clock-time");
  const day = document.querySelector("#clock-date");
  if (clock) clock.textContent = `${time} ET`;
  if (day) day.textContent = `${date} · ${nextMeetingLine(now)}`;
}

function nextMeetingLine(now) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour: "numeric",
    hourCycle: "h23",
  }).formatToParts(now);
  const weekday = parts.find((part) => part.type === "weekday")?.value;
  const hour = Number(parts.find((part) => part.type === "hour")?.value);
  if (weekday === "Sat") return "next meeting Sun 5:00pm";
  if (weekday === "Sun") return hour < 17 ? "next meeting Sun 5:00pm" : "next meeting Mon 8:00am";
  if (weekday === "Fri" && hour >= 17) return "next meeting Sun 5:00pm";
  if (hour < 8) return "next meeting 8:00am";
  if (hour < 17) return "next meeting 5:00pm";
  return "next meeting 8:00am";
}
tickClock();
setInterval(tickClock, 1000);
const STATUS_LABEL = { idle: "Idle", pending: "Waiting", working: "Working now", done: "Done", error: "Error" };

function initials(name) {
  return name.split(" ").map((part) => part[0]).slice(0, 2).join("");
}

function statusOf(name) {
  return state.snapshot.agent_status?.[name] || "idle";
}

function renderChannels() {
  const groups = [{ team: "Floor", agents: [
    { name: "Headquarters", id: "headquarters", role: "Every desk, in one feed." },
    { name: "Active strategy", id: "active", role: "The strategy the floor trader is following." },
    { name: "Strategy library", id: "library", role: "Profitable studies, separated by contract." },
    { name: "Chart", id: "chart", role: "Candles for each futures contract." },
    { name: "R&D", id: "rnd", role: "Research ideas, then the developer runs them." },
    { name: "Engine", id: "engine", role: "The last backtest results for the whole desk." },
    { name: "Book", id: "book", role: "Prop positions and the orders the floor trader sent." },
  ] }];
  for (const agent of state.roster) {
    let group = groups.find((item) => item.team === agent.team);
    if (!group) {
      group = { team: agent.team, agents: [] };
      groups.push(group);
    }
    group.agents.push(agent);
  }
  channelsEl.innerHTML = groups.map((group) => `
    <div class="team-label">${group.team}</div>
    ${group.agents.map((agent) => {
      const key = ["headquarters", "book", "rnd", "engine", "active", "library", "chart"].includes(agent.id) ? agent.id : agent.name;
      const active = state.channel === key;
      const dot = ["headquarters", "book", "rnd", "engine", "active", "library", "chart"].includes(key) ? "" : `<span class="dot ${statusOf(agent.name)}"></span>`;
      const lines = state.snapshot.agent_logs?.[agent.name];
      const ownLine = lines && lines.length ? lines[lines.length - 1] : "";
      const taskText = ownLine || state.snapshot.agent_tasks?.[agent.name] || "";
      const model = agent.model ? `<small class="model">${escapeHtml(agent.model)}</small>` : "";
      const task = taskText ? `<small>${escapeHtml(taskText)}</small>` : "";
      return `<button class="channel ${active ? "active" : ""}" data-channel="${key}" data-team="${group.team}" data-role="${agent.role || ""}">${dot}<span># ${escapeHtml(agent.name.toLowerCase())}${model}${task}</span></button>`;
    }).join("")}
  `).join("");
}

function messagesForChannel() {
  const messages = state.snapshot.messages || [];
  if (state.channel === "headquarters") return messages;
  return messages.filter((message) => message.author === state.channel || message.channel === state.channel);
}

function renderFeed() {
  const pages = {
    headquarters: { name: "headquarters", team: "Floor", role: "Every desk, in one feed." },
    book: { name: "book", team: "Core Trading", role: "Prop positions and the orders the floor trader sent." },
    engine: { name: "engine", team: "Technology & Operations", role: "The last backtest results for the whole desk." },
    rnd: { name: "rnd", team: "Research", role: "Research ideas, then the developer runs them." },
    active: { name: "active", team: "Floor", role: "The strategy the floor trader is following." },
    library: { name: "strategy library", team: "Research", role: "Profitable studies, separated by contract." },
    chart: { name: "chart", team: "Floor", role: "Candles for each futures contract." },
  };
  const current = pages[state.channel] || state.roster.find((agent) => agent.name === state.channel);
  const deskPage = ["book", "engine", "rnd", "active", "library", "chart"].includes(state.channel);
  document.querySelector("#feed").hidden = deskPage;
  const lead = state.book.lead;
  const leadEl = document.querySelector("#lead");
  const paper = state.book.paper || {};
  const paperEl = document.querySelector("#hq-paper");
  paperEl.hidden = state.channel !== "headquarters";
  document.querySelector("#paper-pnl").textContent = money(paper.active_pnl);
  document.querySelector("#paper-dd").textContent = money(paper.drawdown);
  document.querySelector("#paper-equity").textContent = paper.equity == null
    ? "Paper account"
    : `Equity ${money(paper.equity)}`;
  leadEl.hidden = state.channel !== "headquarters";
  const rulesEl = document.querySelector("#hq-rules");
  rulesEl.hidden = state.channel !== "headquarters";
  const recorded = state.book?.headquarters_rules;
  const rules = state.book?.account_rules;
  const rulesLine = rules
    ? `${rules.account || "Account"}: ${money(rules.size)} account, profit target ${money(rules.profit_target)}, ${rules.trailing ? "trailing" : "fixed"} drawdown ${money(rules.max_drawdown)}, max contracts ${rules.max_contracts}.`
    : "";
  document.querySelector("#hq-rules-body").textContent = [rulesLine, recorded?.text].filter(Boolean).join("\n\n")
    || "Enter the account size, profit target, and drawdown. The floor trader uses those figures.";
  renderAccountRules();
  if (lead && lead.pine) {
    document.querySelector("#lead-title").textContent = lead.title || "Lead strategy";
    document.querySelector("#lead-note").textContent =
      `${lead.timeframe || "5m"} chart. ${lead.trades ?? ""} trades, profit ${lead.net_profit ?? ""}, drawdown ${lead.max_drawdown ?? ""}.`
      + (lead.take_profit ? ` Take profit ${lead.take_profit}.` : "")
      + ` ${lead.formula || "Paste this script into TradingView."}`;
    document.querySelector("#lead-pine").textContent = lead.pine;
  } else {
    document.querySelector("#lead-title").textContent = "No strategy script yet";
    document.querySelector("#lead-note").textContent = "The most profitable rule the researchers have cleared stays here. Every desk can read this script.";
    document.querySelector("#lead-pine").textContent = "";
  }
  renderLeaders();
  document.querySelector("#book").hidden = state.channel !== "book";
  document.querySelector("#engine").hidden = state.channel !== "engine";
  document.querySelector("#rnd").hidden = state.channel !== "rnd";
  document.querySelector("#active").hidden = state.channel !== "active";
  document.querySelector("#library").hidden = state.channel !== "library";
  document.querySelector("#chart").hidden = state.channel !== "chart";
  if (state.channel === "chart") ensureChart();
  if (state.channel === "library") ensureLibrary().then(() => renderLibrary());
  renderActive();
  document.querySelector("#composer").hidden = state.channel !== "headquarters";
  titleEl.textContent = state.channel === "library" ? "# strategy library" : `# ${state.channel.toLowerCase()}`;
  teamEl.textContent = current?.team || "Floor";
  roleEl.textContent = current?.role || "Every desk, in one feed.";

  const run = state.snapshot.run;
  const activity = state.snapshot.activity || {};
  const research = state.snapshot.research || "idle";
  const researching = research === "running" || research === "stopping";
  const studying = Boolean(activity.agent && activity.task);
  if (!researching) stopSent = false;
  stopBtn.hidden = !researching;
  startBtn.hidden = researching;
  startBtn.disabled = false;
  renderMeeting(Boolean(run && run.status === "running" && (run.date === "8:00am" || run.date === "5:00pm")));
  if (research === "stopping") {
    pillEl.textContent = "Stopping";
    nowText.textContent = "Stopping the researchers.";
    stopBtn.disabled = true;
  } else if (run && run.status === "running" && (run.date === "8:00am" || run.date === "5:00pm")) {
    const spoken = meetingLines();
    const last = spoken[spoken.length - 1];
    pillEl.textContent = run.date;
    nowText.textContent = last
      ? `${last.author}: ${last.text}`
      : "The meeting is opening.";
  } else if (run && run.status === "running") {
    pillEl.textContent = `Working ${run.contract || run.ticker || ""}`;
    const who = activity.agent ? `${activity.agent} — ` : "";
    nowText.textContent = `${who}${activity.task || "Working"} · ${run.contract || run.ticker || ""}`;
  } else if (studying) {
    pillEl.textContent = "Working";
    nowText.textContent = `${activity.agent} — ${activity.task}`;
  } else if (run && run.status === "stopped") {
    const label = run.contract || run.ticker || "";
    pillEl.textContent = label ? `stopped · ${label}` : "stopped";
    nowText.textContent = `Stopped ${label}. The strategy was not changed.`;
  } else if (research === "stopped") {
    pillEl.textContent = "stopped";
    nowText.textContent = "Researchers stopped. The strategy was not changed.";
    stopBtn.disabled = false;
  } else if (run && run.status === "done") {
    pillEl.textContent = "done";
    nowText.textContent = "The last meeting finished. The engine page lists the tests the developer ran.";
  } else if (research === "running") {
    pillEl.textContent = "Working";
    nowText.textContent = activity.agent ? `${activity.agent} — ${activity.task}` : "The researchers are running the Pine scripts.";
    stopBtn.disabled = false;
  } else {
    pillEl.textContent = "Idle";
    nowText.textContent = "Upload the TradingView bar file, then develop the strategy.";
  }

  const messages = messagesForChannel();
  feedEl.innerHTML = messages.slice(-80).map((message) => `
    <article class="msg ${message.kind || ""}">
      <div class="avatar">${initials(message.author || "S")}</div>
      <div>
        <header><strong></strong><time>${message.time || ""}</time><span class="kind">${message.kind || ""}</span></header>
        <p></p>
      </div>
    </article>
  `).join("");
  feedEl.querySelectorAll(".msg").forEach((node, index) => {
    const message = messages.slice(-80)[index];
    node.querySelector("strong").textContent = message.author || "System";
    node.querySelector("p").textContent = message.text || "";
  });
  const last = [...messages].reverse().find((message) => message.kind === "report" || message.kind === "decision");
  if (state.channel !== "headquarters" && state.channel !== "book") {
    const owned = state.snapshot.reports || {};
    const agent = state.roster.find((item) => item.name === state.channel);
    reportEl.textContent = (agent && owned[agent.report]) || last?.text || "No report from this desk yet.";
  } else if (last) {
    reportEl.textContent = last.text;
  }

  floorEl.innerHTML = state.roster.map((agent) => {
    const status = statusOf(agent.name);
    return `<span class="chip ${status}">${agent.name} · ${STATUS_LABEL[status] || status}</span>`;
  }).join("");

  const chatLines = (state.snapshot.messages || []).filter((message) => message.channel === "Portfolio Manager" && (message.kind === "chat" || message.author === "You"));
  const shown = chatLines.slice(-6);
  document.querySelector("#chat-log").innerHTML = shown.map(() => `<p class="chat-line"><strong></strong><span></span></p>`).join("")
    || `<p class="chat-line">Ask the portfolio manager about the firm, the desk, or an open trade. Chat does not place an order.</p>`;
  document.querySelectorAll("#chat-log .chat-line").forEach((node, index) => {
    const message = shown[index];
    if (!message) return;
    node.querySelector("strong").textContent = message.author;
    node.querySelector("span").textContent = message.text;
  });
}

function renderBook() {
  const settings = state.book.settings || {};
  const mode = document.querySelector("#mode");
  mode.textContent = settings.dry_run
    ? "Dry run: interval alerts are built and not sent"
    : "Live: interval alerts that match the strategy are sent";
  const accounts = (settings.accounts || []).join(", ") || "none yet";
  document.querySelector("#settings-note").textContent =
    `Configured accounts: ${accounts}. Each running strategy uses the account typed on headquarters. CrossTrade ${settings.crosstrade_configured ? "is configured" : "still needs a URL and key"}. `
    + `NinjaTrader confirmation ${settings.nt_configured ? "is configured" : "needs an API token"}. `
    + "TradingView calls /hooks/tradingview/YOUR_TOKEN on a public address. This machine's localhost is not reachable from TradingView.";
  const strategy = state.book.strategy;
  const card = document.querySelector("#strategy-card");
  const rows = strategy?.strategies || [];
  if (!strategy) {
    card.textContent = "No strategy yet. Upload a TradingView bar file with the account size and profit target.";
    document.querySelector("#strategy-list").innerHTML = "";
  } else {
    const active = rows.filter((item) => item.active).map((item) => item.title);
    const running = (state.book.running || []).map((item) => `${item.title} on ${item.account}`);
    const assigned = running.length ? `Running: ${running.join(", ")}. ` : "No strategy is running. ";
    if (!rows.length) {
      card.textContent = `${strategy.timeframe || ""} ${strategy.contract || ""}. ${assigned}${strategy.stance || ""} ${strategy.formula || ""}`.trim();
    } else {
      card.textContent = `${strategy.timeframe || ""} ${strategy.contract || ""}. ${assigned}`
        + `${rows.length} strategies tested. Active: ${active.join(", ") || "none"}. ${strategy.stance}.`;
    }
    document.querySelector("#strategy-list").innerHTML = rows.map((item) => {
      const backtest = item.backtest || {};
      return `<div class="strategy-row">
        <label><input type="checkbox" data-toggle="${item.id}" ${item.active ? "checked" : ""} ${item.proven ? "" : "disabled"} /> ${item.active ? "Live" : "Off"}</label>
        <strong>${item.title}</strong>
        <span>${item.proven ? "proven" : "not proven"}${item.recommended ? " · PM recommended" : ""}${item.accepted ? " · from R&D" : ""} · profit ${backtest.net_profit ?? ""} · drawdown ${backtest.max_drawdown ?? ""}${backtest.drawdown_limit ? ` / limit ${backtest.drawdown_limit}` : ""}</span>
        <button type="button" data-pine="${item.id}">Pine</button>
      </div>`;
    }).join("");
  }
  document.querySelector("#alert-sample").textContent =
`command=place;
account=${(state.book.running && state.book.running[0] && state.book.running[0].account) || "YOUR_ACCOUNT"};
strategy=${(state.book.running && state.book.running[0] && state.book.running[0].title) || "STRATEGY_NAME"};
instrument=${strategy?.contract || "ES1!"};
action={{strategy.order.action}};
qty={{strategy.order.contracts}};
order_type=market;
tif=day;
open={{open}};
high={{high}};
low={{low}};
price={{close}};
time={{time}};
timeframe={{interval}};
sync_strategy=true;
market_position={{strategy.market_position}};
prev_market_position={{strategy.prev_market_position}};
out_of_sync=flatten;`;
  const intervals = state.book.intervals || [];
  document.querySelector("#intervals tbody").innerHTML = intervals.map(() => `<tr><td></td><td></td><td></td><td></td><td></td><td></td></tr>`).join("")
    || `<tr><td colspan="6">No interval alerts yet.</td></tr>`;
  document.querySelectorAll("#intervals tbody tr").forEach((node, index) => {
    const row = intervals[index];
    if (!row) return;
    const cells = node.querySelectorAll("td");
    const result = row.sent ? "sent" : (row.reason || "");
    [row.time, row.price, row.action, row.held, row.target, result].forEach((value, i) => {
      cells[i].textContent = value ?? "";
    });
  });
  const positions = state.book.positions || [];
  document.querySelector("#positions tbody").innerHTML = positions.map((row) => `
    <tr><td></td><td></td><td></td><td></td><td></td><td></td></tr>
  `).join("") || `<tr><td colspan="6">No open futures.</td></tr>`;
  document.querySelectorAll("#positions tbody tr").forEach((node, index) => {
    const row = positions[index];
    if (!row) return;
    const cells = node.querySelectorAll("td");
    [row.account, row.instrument, row.contracts, row.average_price ?? "", row.last_price ?? "", row.pnl ?? ""].forEach((value, i) => {
      cells[i].textContent = value;
    });
  });
  const orders = state.book.orders || [];
  document.querySelector("#orders tbody").innerHTML = orders.map(() => `<tr><td></td><td></td><td></td><td></td><td></td><td></td></tr>`).join("")
    || `<tr><td colspan="6">No orders yet.</td></tr>`;
  document.querySelectorAll("#orders tbody tr").forEach((node, index) => {
    const row = orders[index];
    if (!row) return;
    const cells = node.querySelectorAll("td");
    const result = row.sent ? "sent" : (row.dry_run ? "dry run" : row.reason);
    [row.time, row.account, row.instrument, row.rating, row.side || "", result].forEach((value, i) => {
      cells[i].textContent = value ?? "";
    });
  });
}

function paintKey() {
  const run = state.snapshot.run || {};
  return JSON.stringify({
    channel: state.channel,
    messages: (state.snapshot.messages || []).length,
    last: (state.snapshot.messages || []).at?.(-1)?.id,
    activity: state.snapshot.activity,
    research: state.snapshot.research,
    run: run.status,
    ticker: run.ticker,
    status: state.snapshot.agent_status,
    tasks: state.snapshot.agent_tasks,
    logs: state.snapshot.agent_logs,
    lab: `${state.lab.status}:${state.lab.attempts}:${(state.lab.trials || []).length}:${state.lab.running}`,
    orders: (state.book.orders || []).length,
    positions: (state.book.positions || []).length,
    lead: `${state.book.lead?.title || ""}:${state.book.lead?.net_profit ?? ""}:${state.book.lead?.timeframe || ""}`,
    leaders: (state.book.leaders || []).map((item) => `${item.id}:${item.net_profit}`).join(","),
    running: (state.book.running || []).map((item) => `${item.id}:${item.account}`).join(","),
    paper: `${state.book.paper?.active_pnl ?? ""}:${state.book.paper?.drawdown ?? ""}`,
    strategy: (state.book.strategy?.strategies || []).map((item) => `${item.id}:${item.active}`).join(","),
    intervals: state.book.interval_count,
  });
}

async function refresh() {
  const [roster, snapshot, book, lab] = await Promise.all([
    fetch("/api/roster").then((response) => response.json()),
    fetch("/api/state").then((response) => response.json()),
    fetch("/api/book").then((response) => response.json()),
    fetch("/api/lab").then((response) => response.json()),
  ]);
  state.roster = roster.agents || [];
  state.snapshot = snapshot;
  state.book = book;
  state.lab = lab;
  const key = paintKey();
  if (key === state.paint) return;
  state.paint = key;
  renderChannels();
  renderFeed();
  renderBook();
  renderLab();
}

function meetingLines() {
  return (state.snapshot.messages || []).filter((message) => message.kind === "speech" || message.kind === "report");
}

function renderMeeting(open) {
  const box = document.querySelector("#meeting-live");
  const lines = document.querySelector("#meeting-lines");
  if (!box || !lines) return;
  box.hidden = !open;
  if (!open) return;
  const spoken = meetingLines().slice(-8);
  lines.innerHTML = spoken.map(() => `<p><strong></strong> <span></span></p>`).join("")
    || "<p>The meeting is opening.</p>";
  lines.querySelectorAll("p").forEach((node, index) => {
    const message = spoken[index];
    if (!message) return;
    const strong = node.querySelector("strong");
    const span = node.querySelector("span");
    if (strong) strong.textContent = message.author || "";
    if (span) span.textContent = message.text || "";
  });
}

function renderChartTabs() {
  const tabs = document.querySelector("#chart-tabs");
  if (!tabs) return;
  tabs.innerHTML = state.charts.map((chart) => `
    <button type="button" class="chart-tab ${chart.id === state.chartId ? "active" : ""}" data-chart="${escapeHtml(chart.id)}">${escapeHtml(chart.contract)} ${escapeHtml(chart.timeframe)}</button>
  `).join("");
}

async function ensureChart() {
  renderChartTabs();
  const chart = state.charts.find((item) => item.id === state.chartId) || state.charts[0];
  if (!chart) return;
  const response = await fetch(`/api/chart?contract=${encodeURIComponent(chart.contract)}&timeframe=${encodeURIComponent(chart.timeframe)}&limit=160`);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    document.querySelector("#chart-readout").textContent = payload.detail || "The chart did not load.";
    return;
  }
  state.chartData = payload;
  drawCandles(payload);
}

function drawCandles(payload) {
  const canvas = document.querySelector("#chart-canvas");
  const readout = document.querySelector("#chart-readout");
  if (!canvas) return;
  const candles = payload.candles || [];
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#111";
  context.fillRect(0, 0, width, height);
  if (!candles.length) {
    context.fillStyle = "#ddd";
    context.font = "16px Segoe UI, sans-serif";
    context.fillText(payload.note || "No candles.", 24, 40);
    if (readout) readout.textContent = payload.note || "";
    return;
  }
  const pad = { top: 16, right: 72, bottom: 28, left: 12 };
  const lows = candles.map((candle) => candle.l);
  const highs = candles.map((candle) => candle.h);
  const min = Math.min(...lows);
  const max = Math.max(...highs);
  const span = max - min || 1;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const slot = plotW / candles.length;
  const y = (price) => pad.top + ((max - price) / span) * plotH;
  context.strokeStyle = "#333";
  context.fillStyle = "#aaa";
  context.font = "12px Segoe UI, sans-serif";
  for (let step = 0; step <= 4; step += 1) {
    const price = min + (span * step) / 4;
    const yy = y(price);
    context.beginPath();
    context.moveTo(pad.left, yy);
    context.lineTo(width - pad.right, yy);
    context.stroke();
    context.fillText(price.toFixed(2), width - pad.right + 6, yy + 4);
  }
  const orders = payload.orders || [];
  candles.forEach((candle, index) => {
    const x = pad.left + index * slot + slot / 2;
    const up = candle.c >= candle.o;
    context.strokeStyle = up ? "#3dd68c" : "#ff6b6b";
    context.fillStyle = context.strokeStyle;
    context.beginPath();
    context.moveTo(x, y(candle.h));
    context.lineTo(x, y(candle.l));
    context.stroke();
    const bodyTop = y(Math.max(candle.o, candle.c));
    const bodyBottom = y(Math.min(candle.o, candle.c));
    context.fillRect(x - Math.max(1, slot * 0.3), bodyTop, Math.max(2, slot * 0.6), Math.max(1, bodyBottom - bodyTop));
    orders.filter((order) => order.t === candle.t).forEach((order) => {
      context.fillStyle = order.side === "BUY" ? "#3dd68c" : "#ff6b6b";
      const marker = order.side === "BUY" ? y(candle.l) + 10 : y(candle.h) - 10;
      context.beginPath();
      context.arc(x, marker, 4, 0, Math.PI * 2);
      context.fill();
    });
  });
  const last = candles[candles.length - 1];
  const first = candles[0];
  context.fillStyle = "#ddd";
  context.fillText(first.et, pad.left, height - 8);
  context.fillText(last.et, width - 210, height - 8);
  if (readout) {
    readout.textContent = `${payload.contract} ${payload.timeframe}  ${last.et} ET  O ${last.o}  H ${last.h}  L ${last.l}  C ${last.c}  V ${last.v}  ${orders.length} orders on this window`;
  }
}

function renderAccountRules() {
  const form = document.querySelector("#account-rules");
  if (!form || state.channel !== "headquarters") return;
  const rules = state.book.account_rules;
  if (!rules || form.dataset.editing === "1") return;
  const stamp = [rules.account, rules.size, rules.profit_target, rules.max_drawdown, rules.trailing, rules.max_contracts].join("|");
  if (form.dataset.stamp === stamp) return;
  form.dataset.stamp = stamp;
  form.account.value = rules.account || "";
  form.size.value = rules.size ?? "";
  form.profit_target.value = rules.profit_target ?? "";
  form.max_drawdown.value = rules.max_drawdown ?? "";
  form.trailing.checked = rules.trailing !== false;
  form.max_contracts.value = rules.max_contracts ?? "";
}

function renderLeaders() {
  const list = document.querySelector("#leader-list");
  const status = document.querySelector("#leader-status");
  if (!list) return;
  const leaders = state.book.leaders || [];
  const running = state.book.running || [];
  if (status) {
    status.textContent = running.length
      ? running.map((row) => `${row.title} on ${row.account}`).join(" · ")
      : "No strategy is running.";
  }
  const stamp = `${leaders.map((row) => `${row.id}:${row.net_profit}`).join("|")}|${running.map((row) => `${row.id}:${row.account}`).join("|")}`;
  if (list.dataset.stamp === stamp) return;
  list.dataset.stamp = stamp;
  list.innerHTML = leaders.map((row) => {
    const live = running.find((item) => item.id === row.id);
    const account = live ? live.account : "";
    return `<article class="leader-card" data-id="${escapeHtml(row.id)}">
      <h3><button type="button" class="script-link" data-script="${escapeHtml(row.id)}">${escapeHtml(row.title)}</button></h3>
      <p>${escapeHtml(row.timeframe || "")} · profit ${money(row.net_profit)} · drawdown ${money(row.max_drawdown)} · ${row.trades ?? 0} trades</p>
      <p>${escapeHtml(row.charts || "")}</p>
      <label>Account <input data-account value="${escapeHtml(account)}" placeholder="Account name" maxlength="64" ${live ? "readonly" : ""} /></label>
      <button type="button" data-run="${live ? "stop" : "start"}">${live ? "Stop" : "Run"}</button>
      <pre class="alert-msg">${escapeHtml(strategyAlert(row.title, account))}</pre>
    </article>`;
  }).join("") || "<p>No strategy has finished green on two charts yet.</p>";
}

async function openStrategy(id, source) {
  const modal = document.querySelector("#script-modal");
  const title = document.querySelector("#script-title");
  const body = document.querySelector("#script-body");
  const note = document.querySelector("#script-note");
  modal.hidden = false;
  title.textContent = "Strategy";
  body.value = "";
  note.textContent = "";
  const path = source === "library" ? "/api/library/" : "/api/leaders/";
  const response = await fetch(`${path}${encodeURIComponent(id)}`);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    note.textContent = payload.detail || "That script could not be opened.";
    return;
  }
  title.textContent = payload.title || "Strategy";
  body.value = payload.pine || "";
}

function closeStrategy() {
  document.querySelector("#script-modal").hidden = true;
}

document.querySelector("#script-close").addEventListener("click", closeStrategy);
document.querySelector("#script-modal").addEventListener("click", (event) => {
  if (event.target.id === "script-modal") closeStrategy();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeStrategy();
});
document.querySelector("#script-copy").addEventListener("click", async () => {
  const text = document.querySelector("#script-body").value;
  const note = document.querySelector("#script-note");
  if (!text) {
    note.textContent = "There is no script to copy.";
    return;
  }
  const body = document.querySelector("#script-body");
  try {
    await navigator.clipboard.writeText(text);
    note.textContent = "Copied.";
    return;
  } catch (error) {
    body.focus();
    body.select();
  }
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (error) {
    copied = false;
  }
  note.textContent = copied ? "Copied." : "Select the script and copy it.";
});

function strategyAlert(title, account) {
  const safe = (value) => String(value || "").replace(/\\/g, "\\\\").replace(/"/g, "\\\"");
  return `{"account":"${safe(account || "YOUR_ACCOUNT")}","instrument":"ES1!","strategy":"${safe(title)}","action":"{{strategy.order.action}}","qty":{{strategy.order.contracts}},"order_type":"MARKET","tif":"DAY","destination":"tradovate","price":{{close}},"timeframe":"{{interval}}","time":"{{time}}"}`;
}

async function ensureLibrary() {
  const response = await fetch("/api/library?contract=all");
  const payload = await response.json().catch(() => ({}));
  state.libraryRows = payload.strategies || [];
  const select = document.querySelector("#library-contract");
  if (select && !select.dataset.filled) {
    const shelves = payload.contracts || [];
    select.innerHTML = shelves.map((item) => `<option value="${escapeHtml(item.contract)}">${escapeHtml(item.label)} (${item.count})</option>`).join("");
    select.value = shelves.some((item) => item.contract === "ES1!") ? "ES1!" : (shelves[0]?.contract || "ES1!");
    select.dataset.filled = "1";
  }
  renderLibrary();
}

function renderLibrary() {
  const body = document.querySelector("#library-table tbody");
  const empty = document.querySelector("#library-empty");
  if (!body) return;
  const contract = document.querySelector("#library-contract")?.value || "ES1!";
  const query = (document.querySelector("#library-search")?.value || "").trim().toLowerCase();
  let rows = state.libraryRows.filter((row) => row.contract === contract);
  if (query) {
    rows = rows.filter((row) => row.title.toLowerCase().includes(query) || String(row.timeframe || "").toLowerCase().includes(query));
  }
  const { key, dir } = state.librarySort;
  const textKeys = new Set(["title", "timeframe", "contract_label"]);
  rows.sort((left, right) => {
    const a = left[key];
    const b = right[key];
    const cmp = textKeys.has(key)
      ? String(a ?? "").localeCompare(String(b ?? ""))
      : Number(a ?? 0) - Number(b ?? 0);
    return dir === "asc" ? cmp : -cmp;
  });
  body.innerHTML = rows.map((row) => `<tr data-library="${escapeHtml(row.id)}">
    <td>${escapeHtml(row.title)}</td>
    <td>${escapeHtml(row.contract_label)}</td>
    <td>${escapeHtml(row.timeframe || "")}</td>
    <td>${money(row.net_profit)}</td>
    <td>${money(row.max_drawdown)}</td>
    <td>${row.trades ?? ""}</td>
    <td>${row.win_rate == null ? "" : `${row.win_rate}%`}</td>
  </tr>`).join("");
  if (empty) {
    const label = document.querySelector("#library-contract")?.selectedOptions?.[0]?.textContent || "this contract";
    empty.textContent = rows.length ? "" : `No profitable strategy is filed under ${label}.`;
  }
}

function renderActive() {
  const list = document.querySelector("#active-list");
  if (!list || state.channel !== "active") return;
  const rows = state.book.active_strategies || [];
  const stamp = rows.map((row) => `${row.id}:${row.locked}:${row.account}`).join("|");
  if (list.dataset.stamp === stamp) return;
  list.dataset.stamp = stamp;
  const named = state.book.account_rules?.account;
  const accounts = [...new Set(["paper", named, ...(state.book.settings?.accounts || [])].filter(Boolean))];
  list.innerHTML = rows.map((row) => `
    <article class="active-card" data-id="${escapeHtml(row.id)}">
      <div class="row">
        <label>Account
          <select data-account ${row.locked ? "disabled" : ""}>
            ${accounts.map((account) => `<option ${account === row.account ? "selected" : ""}>${escapeHtml(account)}</option>`).join("")}
          </select>
        </label>
        <button type="button" data-submit ${row.locked ? "disabled" : ""}>Submit</button>
        <button type="button" data-remove>Remove</button>
      </div>
      <textarea ${row.locked ? "readonly" : ""}>${escapeHtml(row.text || "")}</textarea>
    </article>
  `).join("") || "<p>No active strategy. Add one for the floor trader to follow.</p>";
}

async function saveActive(rows) {
  const note = document.querySelector("#active-note");
  let response;
  try {
    response = await fetch("/api/active-strategies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategies: rows }),
    });
  } catch (error) {
    if (note) note.textContent = "The strategy was not saved.";
    return;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (note) note.textContent = payload.detail || "The strategy was not saved.";
    return;
  }
  if (note) note.textContent = "Saved on the desk.";
  const list = document.querySelector("#active-list");
  if (list) delete list.dataset.stamp;
  state.paint = "";
  refresh();
}

function cardsFromDom() {
  return [...document.querySelectorAll("#active-list .active-card")].map((card) => ({
    id: card.dataset.id,
    account: card.querySelector("[data-account]").value,
    text: card.querySelector("textarea").value,
    title: (card.querySelector("textarea").value.match(/strategy\("([^"]+)"/) || [])[1] || "Strategy",
    locked: card.querySelector("textarea").hasAttribute("readonly"),
  }));
}

function renderLab() {
  const lab = state.lab || {};
  const status = document.querySelector("#engine-status");
  if (status) {
    const count = (lab.trials || []).length;
    status.textContent = lab.running
      ? "The developer is running the engine."
      : count
        ? `The developer ran ${count} test${count === 1 ? "" : "s"}.`
        : "The developer has not run a test yet.";
  }
  const accepted = lab.accepted || [];
  const acceptedEl = document.querySelector("#engine-accepted");
  if (acceptedEl) {
    acceptedEl.innerHTML = accepted.map((item) => {
      const backtest = item.backtest || {};
      const frames = (item.frames || []).map((frame) => `${frame.timeframe} profit ${frame.net_profit} drawdown ${frame.max_drawdown}`).join("; ");
      return `<p>${item.title}: profit ${backtest.net_profit}, drawdown ${backtest.max_drawdown}, trades ${backtest.trades}. Eligible for the vote. You turn the winner live.</p>${frames ? `<p>${frames}</p>` : ""}`;
    }).join("") || "<p>Nothing has cleared the trailing floor yet.</p>";
  }
  const playbook = lab.playbook || [];
  const playbookEl = document.querySelector("#engine-playbook");
  if (playbookEl) {
    playbookEl.innerHTML = playbook.map((item) => {
      const backtest = item.backtest || {};
      return `<p>${item.title}: profit ${backtest.net_profit}, drawdown ${backtest.max_drawdown}. ${item.why || ""}</p>`;
    }).join("") || "<p>No rule has reached the profit target inside the trailing floor yet.</p>";
  }
  const scrapped = lab.scrapped || [];
  const scrappedEl = document.querySelector("#engine-scrapped");
  if (scrappedEl) {
    scrappedEl.innerHTML = scrapped.map((item) => `<p>${item.name}: best ${item.best} at ${item.net_profit} after ${item.changes} changes.</p>`).join("") || "<p>Nothing scrapped yet.</p>";
  }
  const trials = lab.trials || [];
  const body = document.querySelector("#engine-trials tbody");
  if (body) {
    body.innerHTML = trials.map(() => "<tr><td></td><td></td><td></td><td></td><td></td></tr>").join("")
      || `<tr><td colspan="5">No trials yet.</td></tr>`;
    body.querySelectorAll("tr").forEach((node, index) => {
      const row = trials[index];
      if (!row) return;
      const cells = node.querySelectorAll("td");
      [row.title, row.net_profit, row.max_drawdown, row.trades, row.proven ? "proven" : "not proven"].forEach((value, i) => {
        cells[i].textContent = value ?? "";
      });
    });
  }
  const boardEl = document.querySelector("#rnd-board");
  if (boardEl) {
    const slate = lab.slate || [];
    const studying = Boolean(lab.studying);
    const current = studying ? slate[0] : null;
    const upcoming = studying ? slate.slice(1, 3) : slate.slice(0, 2);
    const now = current
      ? `<h3>Now</h3><p>${escapeHtml(current.title)}</p>`
      : `<h3>Now</h3><p>No strategy is being studied.</p>`;
    const next = upcoming.length
      ? `<h3>Up next</h3>${upcoming.map((item) => `<p>${escapeHtml(item.title)}</p>`).join("")}`
      : `<h3>Up next</h3><p>No strategies are waiting.</p>`;
    boardEl.innerHTML = now + next;
  }
}

async function runLab() {
  const response = await fetch("/api/lab/run", { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  const status = document.querySelector("#engine-status");
  if (!response.ok && status) status.textContent = payload.detail || "The engine did not start.";
  state.paint = "";
  refresh();
}

channelsEl.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-channel]");
  if (!button) return;
  state.channel = button.dataset.channel;
  state.paint = "";
  renderChannels();
  renderFeed();
});

document.querySelector("#strategy-list").addEventListener("change", async (event) => {
  const id = event.target.dataset.toggle;
  if (!id) return;
  const response = await fetch(`/api/strategies/${encodeURIComponent(id)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active: event.target.checked }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    document.querySelector("#strategy-card").textContent = payload.detail || "That strategy could not be changed.";
    return;
  }
  state.paint = "";
  refresh();
});

document.querySelector("#strategy-list").addEventListener("click", async (event) => {
  const id = event.target.dataset.pine;
  if (!id) return;
  const response = await fetch(`/api/strategies/${encodeURIComponent(id)}`);
  const payload = await response.json().catch(() => ({}));
  document.querySelector("#pine-view").textContent = response.ok ? payload.pine || "" : (payload.detail || "Pine script is not available.");
});

document.querySelector("#composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = new FormData(form);
  const response = await fetch("/api/research", { method: "POST", body });
  const payload = await response.json().catch(() => ({}));
  const detail = typeof payload.detail === "string" ? payload.detail : "The bar file could not be tested.";
  document.querySelector("#hint").textContent = response.ok
    ? `The desks are discussing ${payload.bars || ""} bars and backtesting each idea.`
    : detail;
  if (!response.ok) return;
  state.paint = "";
  refresh();
});

let stopSent = false;
stopBtn.addEventListener("click", async () => {
  if (stopSent) return;
  stopSent = true;
  stopBtn.disabled = true;
  nowText.textContent = "Stopping the researchers.";
  pillEl.textContent = "Stopping";
  try {
    const response = await fetch("/api/runs/stop", { method: "POST" });
    if (!response.ok) throw new Error("stop failed");
  } catch (error) {
    stopSent = false;
    stopBtn.disabled = false;
    nowText.textContent = "Stop did not reach the desk. Try the button again.";
    return;
  }
  state.paint = "";
  refresh();
});

startBtn.addEventListener("click", async () => {
  startBtn.disabled = true;
  nowText.textContent = "Starting the researchers.";
  pillEl.textContent = "Starting";
  try {
    const response = await fetch("/api/runs/start", { method: "POST" });
    if (!response.ok) throw new Error("start failed");
  } catch (error) {
    startBtn.disabled = false;
    nowText.textContent = "Start did not reach the desk. Try the button again.";
    return;
  }
  state.paint = "";
  refresh();
});

document.querySelector("#library-table").addEventListener("click", (event) => {
  const sort = event.target.closest("th[data-sort]");
  if (sort) {
    const key = sort.dataset.sort;
    if (state.librarySort.key === key) state.librarySort.dir = state.librarySort.dir === "asc" ? "desc" : "asc";
    else state.librarySort = { key, dir: key === "title" ? "asc" : "desc" };
    renderLibrary();
    return;
  }
  const row = event.target.closest("tr[data-library]");
  if (row) openStrategy(row.dataset.library, "library");
});
document.querySelector("#library-contract").addEventListener("change", renderLibrary);
document.querySelector("#library-search").addEventListener("input", renderLibrary);

document.querySelector("#chart-tabs").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-chart]");
  if (!tab) return;
  state.chartId = tab.dataset.chart;
  state.chartData = null;
  ensureChart();
});
document.querySelector("#chart-add").addEventListener("click", () => {
  document.querySelector("#chart-open").hidden = false;
});
document.querySelector("#chart-open").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  const contract = form.contract.value;
  const timeframe = form.timeframe.value;
  const id = `${contract}-${timeframe}-${Date.now()}`;
  state.charts.push({ id, contract, timeframe });
  state.chartId = id;
  form.hidden = true;
  ensureChart();
});

document.querySelector("#lab-run").addEventListener("click", runLab);
document.querySelector("#rnd-run").addEventListener("click", runLab);

const rulesForm = document.querySelector("#account-rules");
rulesForm.addEventListener("focusin", () => {
  rulesForm.dataset.editing = "1";
});
rulesForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const response = await fetch("/api/account-rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      account: form.account.value.trim(),
      size: Number(form.size.value),
      profit_target: Number(form.profit_target.value),
      max_drawdown: Number(form.max_drawdown.value),
      trailing: form.trailing.checked,
      max_contracts: Number(form.max_contracts.value),
    }),
  });
  const payload = await response.json().catch(() => ({}));
  const note = document.querySelector("#account-rules-note");
  if (note) note.textContent = response.ok ? "The floor trader has these account rules." : (payload.detail || "The account rules were not saved.");
  delete form.dataset.stamp;
  delete form.dataset.editing;
  state.paint = "";
  refresh();
});

document.querySelector("#leader-list").addEventListener("click", async (event) => {
  const script = event.target.closest("[data-script]");
  if (script) {
    openStrategy(script.dataset.script);
    return;
  }
  const button = event.target.closest("[data-run]");
  if (!button) return;
  const card = button.closest(".leader-card");
  const account = card.querySelector("[data-account]").value.trim();
  const response = await fetch("/api/running", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: card.dataset.id, account, enabled: button.dataset.run === "start" }),
  });
  const payload = await response.json().catch(() => ({}));
  const note = document.querySelector("#leader-note");
  if (note) {
    note.textContent = response.ok
      ? (button.dataset.run === "start" ? `${account} is running that strategy.` : "That strategy is stopped.")
      : (payload.detail || "That strategy was not changed.");
  }
  delete document.querySelector("#leader-list").dataset.stamp;
  state.paint = "";
  refresh();
});

document.querySelector("#active-add").addEventListener("click", () => {
  const rows = cardsFromDom();
  rows.push({ id: `strategy-${Date.now()}`, account: "paper", text: "", title: "Strategy", locked: false });
  saveActive(rows);
});

let activeSaveTimer;
document.querySelector("#active-list").addEventListener("input", () => {
  clearTimeout(activeSaveTimer);
  activeSaveTimer = setTimeout(() => saveActive(cardsFromDom()), 500);
});
document.querySelector("#active-list").addEventListener("change", (event) => {
  if (event.target.matches("[data-account]")) saveActive(cardsFromDom());
});

document.querySelector("#active-list").addEventListener("click", (event) => {
  const card = event.target.closest(".active-card");
  if (!card) return;
  if (event.target.matches("[data-remove]")) {
    saveActive(cardsFromDom().filter((row) => row.id !== card.dataset.id));
  }
  if (event.target.matches("[data-submit]")) {
    const rows = cardsFromDom().map((row) => row.id === card.dataset.id ? { ...row, locked: true } : row);
    saveActive(rows);
  }
});

document.querySelector("#review").addEventListener("click", async () => {
  const response = await fetch("/api/review", { method: "POST" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    document.querySelector("#meeting-note").textContent = body.detail || "The review did not start.";
  }
});

document.querySelector("#chat").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = event.target.message;
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const log = document.querySelector("#chat-log");
    const line = document.createElement("p");
    line.className = "chat-line";
    line.textContent = body.detail || "The portfolio manager did not receive that.";
    log.appendChild(line);
  }
  state.paint = "";
  refresh();
});

refresh();
setInterval(refresh, 1200);

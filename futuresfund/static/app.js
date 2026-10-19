const state = {
  channel: "headquarters",
  roster: [],
  snapshot: { messages: [], agent_status: {}, reports: {}, run: null, activity: {} },
  book: { positions: [], orders: [], settings: {} },
  lab: { messages: [], trials: [], accepted: [], scrapped: [], status: "idle" },
  charts: [
    { id: "es-5m", contract: "ES1!", timeframe: "5m", follow: true },
    { id: "nq-5m", contract: "NQ1!", timeframe: "5m", follow: true },
    { id: "gc-5m", contract: "GC1!", timeframe: "5m", follow: true },
  ],
  chartId: "es-5m",
  chartData: null,
  libraryRows: [],
  engineId: "1",
  librarySort: { key: "net_profit", dir: "desc" },
  untestedSort: { key: "net_profit", dir: "desc" },
  paint: "",
};

const channelsEl = document.querySelector("#channels");
const feedEl = document.querySelector("#feed");
const floorEl = document.querySelector("#floor");
const titleEl = document.querySelector("#channel-title");
const teamEl = document.querySelector("#channel-team");
const roleEl = document.querySelector("#channel-role");
const pillEl = document.querySelector("#run-pill");
const holdBtn = document.querySelector("#hold-new");
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
window.addEventListener("resize", () => {
  if (state.channel === "chart" && state.chartData) {
    fitChartCanvas();
    drawCandles(state.chartData);
  }
});
setInterval(tickClock, 1000);
const STATUS_LABEL = { idle: "Idle", pending: "Waiting", working: "Working now", done: "Done", error: "Error" };

function initials(name) {
  return name.split(" ").map((part) => part[0]).slice(0, 2).join("");
}

function statusOf(name) {
  return state.snapshot.agent_status?.[name] || "idle";
}

function dutyList(agent) {
  if (Array.isArray(agent.responsibilities) && agent.responsibilities.length) return agent.responsibilities;
  return String(agent.role || "").split(/(?<=\.)\s+/).map((line) => line.trim()).filter(Boolean);
}

function hideAgentTip() {
  const tip = document.querySelector("#agent-tip");
  if (tip) tip.hidden = true;
}

function showAgentTip(button) {
  const tip = document.querySelector("#agent-tip");
  if (!tip || !button?.dataset.duties) return;
  let duties = [];
  try {
    duties = JSON.parse(decodeURIComponent(button.dataset.duties));
  } catch (error) {
    duties = [];
  }
  if (!duties.length) {
    hideAgentTip();
    return;
  }
  const name = button.dataset.name || "";
  tip.innerHTML = `<strong>${escapeHtml(name)}</strong><ul>${duties.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`;
  tip.hidden = false;
  const rect = button.getBoundingClientRect();
  const width = tip.offsetWidth;
  const height = tip.offsetHeight;
  let left = rect.right + 10;
  if (left + width > window.innerWidth - 8) left = Math.max(8, rect.left - width - 10);
  let top = rect.top;
  if (top + height > window.innerHeight - 8) top = Math.max(8, window.innerHeight - height - 8);
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function renderChannels() {
  const hoveredName = agentTipButton?.dataset?.name || "";
  const groups = [{ team: "Floor", agents: [
    { name: "Headquarters", id: "headquarters", role: "Every desk, in one feed.", responsibilities: ["Every desk, in one feed", "Start, stop, and hold research from here"] },
    { name: "Active strategy", id: "active", role: "The strategies the floor trader and the quantitative trader execute.", responsibilities: ["Strategies the floor trader and the quantitative trader execute", "Paper or live is chosen on this page"] },
    { name: "Strategy library", id: "library", role: "Most profitable runs are filed after a profitable five-year test. One-year runs wait until then.", responsibilities: ["Acceptance numbers live here", "A run is filed after a profitable five-year test", "One-year runs wait until then"] },
    { name: "Indicator library", id: "indicators", role: "Indicators every desk can build and test strategies from.", responsibilities: ["Indicators every desk can build from", "Strategies are tested from these indicators"] },
    { name: "Chart", id: "chart", role: "Candles for each futures contract.", responsibilities: ["Candles for each futures contract"] },
    { name: "R&D", id: "rnd", role: "Research ideas, then the developer runs them.", responsibilities: ["Research ideas", "The developer runs them"] },
    { name: "Under the hood", id: "engine", role: "Four PineTS engines, one for each developer.", responsibilities: ["Four PineTS engines, one for each developer", "Restart stops a hung engine", "Skip leaves the current script and takes the next one"] },
    { name: "Book", id: "book", role: "Prop positions and the orders the floor trader sent.", responsibilities: ["Prop positions", "Orders the floor trader sent"] },
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
      const key = ["headquarters", "book", "rnd", "engine", "active", "library", "indicators", "chart"].includes(agent.id) ? agent.id : agent.name;
      const active = state.channel === key;
      const dot = ["headquarters", "book", "rnd", "engine", "active", "library", "indicators", "chart"].includes(key) ? "" : `<span class="dot ${statusOf(agent.name)}"></span>`;
      const lines = state.snapshot.agent_logs?.[agent.name];
      const ownLine = lines && lines.length ? lines[lines.length - 1] : "";
      const taskText = ownLine || state.snapshot.agent_tasks?.[agent.name] || "";
      const model = agent.model ? `<small class="model">${escapeHtml(agent.model)}</small>` : "";
      const task = taskText ? `<small>${escapeHtml(taskText)}</small>` : "";
      const duties = encodeURIComponent(JSON.stringify(dutyList(agent)));
      return `<button class="channel ${active ? "active" : ""}" data-channel="${key}" data-team="${group.team}" data-name="${escapeHtml(agent.name)}" data-duties="${duties}" data-role="${escapeHtml(agent.role || "")}">${dot}<span># ${escapeHtml(agent.name.toLowerCase())}${model}${task}</span></button>`;
    }).join("")}
  `).join("");
  if (!hoveredName) return;
  const next = [...channelsEl.querySelectorAll("button.channel")].find((button) => button.dataset.name === hoveredName && button.matches(":hover"));
  if (!next) {
    agentTipButton = null;
    hideAgentTip();
    return;
  }
  agentTipButton = next;
  showAgentTip(next);
}

function messagesForChannel() {
  const messages = state.snapshot.messages || [];
  if (state.channel === "headquarters") return messages;
  return messages.filter((message) => message.author === state.channel || message.channel === state.channel);
}

function latestAgentLine(name) {
  const logs = state.snapshot.agent_logs?.[name];
  if (logs && logs.length) return logs[logs.length - 1];
  const task = state.snapshot.agent_tasks?.[name];
  return task || "Waiting.";
}

function renderLiveAgents() {
  const list = document.querySelector("#live-agent-list");
  if (!list) return;
  const agents = state.roster || [];
  const rows = agents.length ? agents : [{ name: "Desks" }];
  let sets = list.querySelectorAll(".ticker-set");
  if (sets.length !== 2) {
    const first = document.createElement("div");
    const second = document.createElement("div");
    first.className = "ticker-set";
    second.className = "ticker-set";
    second.setAttribute("aria-hidden", "true");
    list.replaceChildren(first, second);
    sets = list.querySelectorAll(".ticker-set");
  }
  sets.forEach((set) => {
    if (set.children.length !== rows.length) {
      set.replaceChildren(...rows.map(() => {
        const item = document.createElement("span");
        item.className = "ticker-item";
        const name = document.createElement("strong");
        const line = document.createElement("span");
        item.append(name, line);
        return item;
      }));
    }
    [...set.children].forEach((item, index) => {
      const agent = rows[index];
      item.querySelector("strong").textContent = agent.name;
      item.querySelector("span").textContent = ` — ${latestAgentLine(agent.name)}`;
    });
  });
}

function renderEngineResults(host, results) {
  if (!host) return;
  const rows = (results || []).slice(-20).reverse();
  const signature = JSON.stringify(rows);
  if (host.dataset.signature === signature) return;
  host.dataset.signature = signature;
  const table = document.createElement("table");
  table.className = "hood-result-table";
  const head = document.createElement("thead");
  const titles = document.createElement("tr");
  ["Script", "Time", "Profit", "Drawdown", "Factor", "Ratio", "Trades", "Win"].forEach((label) => {
    const cell = document.createElement("th");
    cell.textContent = label;
    titles.append(cell);
  });
  head.append(titles);
  const body = document.createElement("tbody");
  if (!rows.length) {
    const empty = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.className = "hood-result-empty";
    cell.textContent = "Waiting for a result.";
    empty.append(cell);
    body.append(empty);
  }
  rows.forEach((row) => {
    const line = document.createElement("tr");
    const profit = Number(row.net_profit);
    const factor = Number(row.profit_factor);
    const profitCell = document.createElement("td");
    profitCell.textContent = Number.isFinite(profit) ? money(profit) : "";
    profitCell.className = Number.isFinite(profit) && profit < 0 ? "hood-loss" : "hood-gain";
    const factorCell = document.createElement("td");
    factorCell.textContent = row.profit_factor == null ? "" : String(row.profit_factor);
    if (Number.isFinite(factor) && factor >= 3) factorCell.className = "hood-goal";
    const cells = [
      row.script || "",
      row.time || "",
      profitCell,
      Number.isFinite(Number(row.max_drawdown)) ? money(row.max_drawdown) : "",
      factorCell,
      row.profit_ratio == null ? "" : String(row.profit_ratio),
      row.trades == null ? "" : String(row.trades),
      row.win_rate == null ? "" : `${row.win_rate}%`,
    ];
    cells.forEach((value) => {
      if (value instanceof Node) {
        line.append(value);
        return;
      }
      const cell = document.createElement("td");
      cell.textContent = value;
      line.append(cell);
    });
    body.append(line);
  });
  table.append(head, body);
  host.replaceChildren(table);
}

function progressText(engine) {
  const report = state.lab && state.lab.progress;
  if (!report || !report.ready) return "Counting learning and created strategies.";
  const rows = report.rows || [];
  if (!engine.timeframe) {
    return rows.map((row) => `${row.timeframe} Learning ${row.learning_done}/${row.learning_total} · Created ${row.created_done}/${row.created_total}`).join(" · ")
      || "Counting learning and created strategies.";
  }
  const row = rows.find((item) => item.timeframe === engine.timeframe);
  if (!row) return "Counting learning and created strategies.";
  return `Learning ${row.learning_done}/${row.learning_total} · Created ${row.created_done}/${row.created_total}${Number(engine.attempt) ? ` · attempt ${engine.attempt}` : ""}`;
}

function sourceText(engine) {
  const report = state.lab && state.lab.progress;
  if (!report || !report.ready || !engine.timeframe) return "";
  const row = (report.rows || []).find((item) => item.timeframe === engine.timeframe);
  if (!row) return "";
  const learningLeft = Number(row.learning_open ?? (row.learning_total - row.learning_done));
  const createdLeft = Number(row.created_open ?? (row.created_total - row.created_done));
  if (learningLeft > 0) return " · from the learning folder";
  if (createdLeft > 0) return " · from createdStrategies";
  return " · learning and createdStrategies are finished";
}

function goalLabel(acceptance) {
  const rules = acceptance || {};
  const goal = rules.goal || "min_trades";
  if (goal === "profit_factor") {
    const factor = Number(rules.profit_factor);
    return Number.isFinite(factor) && factor > 0 ? `Profit factor ${factor}` : "Profit factor";
  }
  if (goal === "min_profit") {
    const profit = Number(rules.min_profit);
    return Number.isFinite(profit) && profit >= 0 ? `Profit $${Math.round(profit).toLocaleString("en-US")}` : "Profit";
  }
  if (goal === "min_trades") {
    const trades = Number(rules.min_trades);
    return Number.isFinite(trades) && trades > 0 ? `Trades ${trades}` : "Trades";
  }
  const rate = Number(rules.win_rate);
  return Number.isFinite(rate) && rate > 0 ? `Win rate ${rate}%` : "Win rate";
}

function renderUnderTheHood() {
  const grid = document.querySelector("#hood-grid");
  if (!grid || state.channel !== "engine") return;
  const charts = { "2m": "2-minute", "5m": "5-minute", "15m": "15-minute" };
  const fallback = [
    { id: 1, developer: "2min chart developer", timeframe: "2m", system: "PineTS", busy: false, logs: [] },
    { id: 2, developer: "5 min chart developer", timeframe: "5m", system: "PineTS", busy: false, logs: [] },
    { id: 3, developer: "15 min chart developer", timeframe: "15m", system: "PineTS", busy: false, logs: [] },
    { id: 4, developer: "Creation tester", timeframe: "", system: "PineTS", busy: false, successes: 0, goal: 10, studying: false, study: "", logs: [] },
  ];
  const engines = (state.lab && state.lab.engines && state.lab.engines.length) ? state.lab.engines : fallback;
  const seen = new Set();
  engines.slice(0, 4).forEach((engine) => {
    seen.add(String(engine.id));
    let card = grid.querySelector(`[data-engine="${engine.id}"]`);
    if (!card) {
      card = document.createElement("article");
      card.className = "hood-card";
      card.dataset.engine = String(engine.id);
      const header = document.createElement("header");
      const title = document.createElement("h3");
      const stateLabel = document.createElement("span");
      header.append(title, stateLabel);
      const who = document.createElement("p");
      who.className = "hood-who";
      const scale = document.createElement("div");
      scale.className = "hood-scale";
      const scaleLabels = document.createElement("div");
      scaleLabels.className = "hood-scale-labels";
      const opposite = document.createElement("span");
      opposite.textContent = "Opposite of the goal";
      const goalEnd = document.createElement("span");
      goalEnd.className = "hood-scale-goal";
      goalEnd.textContent = goalLabel((state.book && state.book.strategy_acceptance) || {});
      scaleLabels.append(opposite, goalEnd);
      const track = document.createElement("div");
      track.className = "hood-scale-track";
      const mark = document.createElement("span");
      mark.className = "hood-scale-mark";
      track.append(mark);
      const scaleNote = document.createElement("p");
      scaleNote.className = "hood-scale-note";
      scale.append(scaleLabels, track, scaleNote);
      const tools = document.createElement("div");
      tools.className = "hood-tools";
      const restart = document.createElement("button");
      restart.type = "button";
      restart.className = "hood-skip hood-restart";
      restart.textContent = "Restart";
      const skip = document.createElement("button");
      skip.type = "button";
      skip.className = "hood-skip";
      skip.textContent = "Skip";
      tools.append(restart, skip);
      const results = document.createElement("div");
      results.className = "hood-results";
      card.append(header, who, scale, tools, results);
      grid.append(card);
    }
    card.querySelector("h3").textContent = `Engine ${engine.id}`;
    const stateLabel = card.querySelector("header span");
    stateLabel.className = engine.busy ? "hood-state busy" : "hood-state";
    stateLabel.textContent = engine.busy ? "Running" : (engine.studying ? "Studying" : "Idle");
    const chart = charts[engine.timeframe] || (engine.id === 4 ? "creation" : "chart");
    const script = engine.busy && engine.script ? ` · ${engine.script}` : "";
    card.querySelector(".hood-who").textContent = `${engine.system || "PineTS"} · ${engine.developer || "Developer"} · ${chart}${sourceText(engine)}${script}`;
    let progress = card.querySelector(".hood-progress");
    if (!progress) {
      progress = document.createElement("p");
      progress.className = "hood-progress";
      card.querySelector(".hood-who").insertAdjacentElement("afterend", progress);
    }
    progress.textContent = progressText(engine);
    const running = Boolean(engine.studying || engine.busy);
    const successes = Number(engine.successes || 0);
    if (running) stateLabel.textContent = `${engine.busy ? "Running" : "Studying"} · ${successes} successful`;
    const goalEnd = card.querySelector(".hood-scale-goal");
    if (goalEnd) goalEnd.textContent = goalLabel((state.book && state.book.strategy_acceptance) || {});
    const position = Math.max(0, Math.min(1, Number(engine.scale || 0)));
    const mark = card.querySelector(".hood-scale-mark");
    mark.style.left = `${position * 100}%`;
    const moveText = {
      closer: "Toward the goal",
      farther: "Away from the goal",
      flat: "No helpful change",
      start: "Starting point",
    }[engine.scale_move] || "Waiting for a result";
    const phase = engine.scale_phase ? ` · ${engine.scale_phase}` : "";
    card.querySelector(".hood-scale-note").textContent = `${moveText}${phase}`;
    const restart = card.querySelector(".hood-restart");
    restart.disabled = false;
    if (restart.dataset.sent !== "1") restart.textContent = "Restart";
    const skip = card.querySelector(".hood-skip:not(.hood-restart)");
    skip.disabled = false;
    if (skip.dataset.sent !== "1") skip.textContent = "Skip";
    renderEngineResults(card.querySelector(".hood-results"), engine.results || []);
  });
  grid.querySelectorAll("[data-engine]").forEach((card) => {
    if (!seen.has(card.dataset.engine)) card.remove();
  });
}

let kronosFetched = 0;

async function loadKronosProgress() {
  if (Date.now() - kronosFetched < 1000) return;
  kronosFetched = Date.now();
  try {
    const response = await fetch("/api/kronos-training");
    if (!response.ok) throw new Error(String(response.status));
    paintKronosProgress(await response.json());
  } catch (error) {
    kronosFetched = 0;
  }
}

function paintKronosProgress(row) {
  const phase = document.querySelector("#kronos-phase");
  const count = document.querySelector("#kronos-count");
  const fill = document.querySelector("#kronos-fill");
  const bar = document.querySelector("#kronos-bar");
  const detail = document.querySelector("#kronos-detail");
  if (!count || !fill || !bar) return;
  const wholeDone = Number(row?.whole_done || 0);
  const wholeTotal = Number(row?.whole_total || 0);
  const step = Number(row?.step || 0);
  const steps = Number(row?.steps || 0);
  const percent = wholeTotal
    ? Math.max(0, Math.min(100, Number.isFinite(Number(row?.percent)) ? Number(row.percent) : (wholeDone / wholeTotal) * 100))
    : (steps ? Math.max(0, Math.min(100, (step / steps) * 100)) : 0);
  count.textContent = wholeTotal
    ? `${wholeDone.toLocaleString("en-US")} / ${wholeTotal.toLocaleString("en-US")}`
    : `${step} / ${steps}`;
  fill.style.width = `${percent}%`;
  bar.setAttribute("aria-valuenow", String(Math.round(percent)));
  bar.setAttribute("aria-valuemax", "100");
  if (phase) phase.textContent = wholeTotal ? "Entire run" : "Kronos";
  if (!detail) return;
  detail.textContent = kronosDetail(row, wholeDone, wholeTotal);
}

function kronosDetail(row, wholeDone, wholeTotal) {
  if (row?.finished) return "The fine-tune is complete.";
  if (!row?.running || !wholeTotal) return "Waiting for the first step.";
  const left = Math.max(0, wholeTotal - wholeDone).toLocaleString("en-US");
  const epoch = Number(row.epoch || 0);
  const epochs = Number(row.epochs || 0);
  const step = Number(row.step || 0);
  const steps = Number(row.steps || 0);
  const loss = Number.isFinite(Number(row.loss)) ? ` Loss ${Number(row.loss).toFixed(4)}.` : "";
  if (row.tokenizer_finished && row.phase === "predictor" && !step) {
    return `Tokenizer finished. Predictor has ${epochs} epochs left. The first line is written at step 50. ${left} steps left.`;
  }
  const name = row.phase ? row.phase.charAt(0).toUpperCase() + row.phase.slice(1) : "Training";
  return `${name} epoch ${epoch} of ${epochs}, step ${step} of ${steps}.${loss} ${left} steps left.`;
}

function renderFeed() {
  const pages = {
    headquarters: { name: "headquarters", team: "Floor", role: "Every desk, in one feed." },
    book: { name: "book", team: "Core Trading", role: "Prop positions and the orders the floor trader sent." },
    engine: { name: "under the hood", team: "Technology & Operations", role: "Four PineTS engines, one for each developer." },
    rnd: { name: "rnd", team: "Research", role: "Research ideas, then the developer runs them." },
    active: { name: "active", team: "Floor", role: "The strategies the floor trader and the quantitative trader execute." },
    library: { name: "strategy library", team: "Research", role: "A strategy is filed when the checked boxes are met. Drawdown under 5 percent of the account and the trade count start on." },
    indicators: { name: "indicator library", team: "Research", role: "Indicators every desk can build and test strategies from." },
    chart: { name: "chart", team: "Floor", role: "Candles for each futures contract." },
  };
  const current = pages[state.channel] || state.roster.find((agent) => agent.name === state.channel);
  const deskPage = ["book", "engine", "rnd", "active", "library", "indicators", "chart"].includes(state.channel);
  document.querySelector("#feed").hidden = deskPage;
  const callMeeting = document.querySelector("#call-meeting");
  if (callMeeting) callMeeting.hidden = state.channel !== "headquarters";
  const kronosBar = document.querySelector("#kronos-progress");
  if (kronosBar) {
    const showKronos = state.channel === "Systems Administrator";
    kronosBar.hidden = !showKronos;
    if (showKronos) loadKronosProgress();
  }
  const riskReview = document.querySelector("#risk-review");
  document.querySelector("main")?.classList.toggle("risk-open", state.channel === "Risk Manager");
  if (riskReview) {
    riskReview.hidden = state.channel !== "Risk Manager";
    if (state.channel === "Risk Manager") loadRiskSuggestions();
  }
  renderAccounts();
  renderAcceptance();
  document.querySelector("#book").hidden = state.channel !== "book";
  document.querySelector("#engine").hidden = state.channel !== "engine";
  document.querySelector("#rnd").hidden = state.channel !== "rnd";
  document.querySelector("#active").hidden = state.channel !== "active";
  document.querySelector("#library").hidden = state.channel !== "library";
  document.querySelector("#indicators").hidden = state.channel !== "indicators";
  if (state.channel === "indicators") ensureIndicators();
  document.querySelector("#chart").hidden = state.channel !== "chart";
  if (state.channel === "chart") ensureChart();
  if (state.channel === "library") ensureLibrary().then(() => renderLibrary());
  renderActive();
  paintKronosButton();
  titleEl.textContent = `# ${(current && current.name) || state.channel.toLowerCase()}`;
  teamEl.textContent = current?.team || "Floor";
  roleEl.textContent = current?.role || "Every desk, in one feed.";

  const run = state.snapshot.run;
  const activity = state.snapshot.activity || {};
  const research = state.snapshot.research || "idle";
  const holdingNew = Boolean(state.snapshot.hold_new);
  const researching = research === "running" || research === "stopping";
  const studying = Boolean(activity.agent && activity.task);
  if (!researching) stopSent = false;
  stopBtn.hidden = false;
  startBtn.hidden = false;
  startBtn.disabled = research === "running";
  stopBtn.disabled = !researching;
  if (holdBtn) {
    holdBtn.textContent = holdingNew ? "Make new strategies" : "Hold new strategies";
    holdBtn.classList.toggle("held", holdingNew);
    holdBtn.setAttribute("aria-pressed", holdingNew ? "true" : "false");
    holdBtn.disabled = false;
  }
  renderMeeting(Boolean(run && run.status === "running" && (run.date === "8:00am" || run.date === "5:00pm")));
  if (research === "stopping") {
    pillEl.textContent = "Stopping";
    nowText.textContent = "Stopping the researchers.";
    stopBtn.disabled = true;
  } else if (holdingNew && research === "running") {
    pillEl.textContent = "Holding";
    nowText.textContent = "The strategy team is not writing a new strategy. Each chart finishes the learning folder, then studies createdStrategies.";
    stopBtn.disabled = false;
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
    nowText.textContent = "Press Start to run the Pine scripts.";
  }

  renderLiveAgents();
  renderUnderTheHood();
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

  if (floorEl) {
    floorEl.innerHTML = state.roster.map((agent) => {
      const status = statusOf(agent.name);
      return `<span class="chip ${status}">${agent.name} · ${STATUS_LABEL[status] || status}</span>`;
    }).join("");
  }

  fillChatAgents();
  const desk = document.querySelector("#chat-agent")?.value || "Portfolio Manager";
  const chatLines = (state.snapshot.messages || []).filter((message) =>
    (message.channel === desk || message.author === desk) && (message.kind === "chat" || message.author === "You"));
  const shown = chatLines.slice(-6);
  document.querySelector("#chat-log").innerHTML = shown.map(() => `<p class="chat-line"><strong></strong><span></span></p>`).join("")
    || `<p class="chat-line">Write to a desk. They can hand one written report to another desk. Chat does not place an order.</p>`;
  document.querySelectorAll("#chat-log .chat-line").forEach((node, index) => {
    const message = shown[index];
    if (!message) return;
    node.querySelector("strong").textContent = message.author;
    node.querySelector("span").textContent = message.text;
  });
}

function loadRiskSuggestions() {
  const host = document.querySelector("#risk-suggestions");
  if (!host) return;
  const now = Date.now();
  if (host.dataset.loading === "1" || (host.dataset.at && now - Number(host.dataset.at) < 5000)) return;
  host.dataset.loading = "1";
  fetch("/api/risk-suggestions")
    .then((response) => response.json())
    .then((payload) => {
      host.dataset.at = String(Date.now());
      paintRiskSuggestions(payload.suggestions || []);
    })
    .catch(() => {
      host.textContent = "The suggestions could not be loaded.";
    })
    .finally(() => {
      host.dataset.loading = "0";
    });
}

function paintRiskSuggestions(rows) {
  const host = document.querySelector("#risk-suggestions");
  if (!host) return;
  const rank = { pending: 0, applied: 1, approved: 1, denied: 2 };
  rows = rows.slice().sort((left, right) => (rank[left.status] ?? 1) - (rank[right.status] ?? 1));
  if (!rows.length) {
    host.textContent = "No suggestion yet. The risk manager files one after it reads the last hour of completed backtests.";
    return;
  }
  host.innerHTML = rows.map((row) => `
    <article class="risk-card" data-id="${escapeHtml(row.id || "")}">
      <p></p>
      <p class="role"></p>
      <p class="role"></p>
    </article>
  `).join("");
  host.querySelectorAll(".risk-card").forEach((card, index) => {
    const row = rows[index];
    const lines = card.querySelectorAll("p");
    lines[0].textContent = row.proposed || "";
    lines[1].textContent = row.justification || "";
    const stats = row.stats || {};
    const labels = {
      hour_runs: "completed backtests",
      issue_runs: "with this problem",
      net_profit: "worst profit",
      max_drawdown: "worst drawdown",
      trades_per_year: "most trades a year",
      biggest_loser: "largest loss",
    };
    lines[2].textContent = Object.entries(labels)
      .filter(([key]) => stats[key] !== undefined && stats[key] !== null)
      .map(([key, label]) => `${label} ${stats[key]}`)
      .join(" · ");
    if (row.status === "applied" || row.status === "approved") {
      const note = document.createElement("p");
      note.className = "role";
      note.textContent = "Added to the research guide.";
      card.appendChild(note);
      return;
    }
    if (row.status === "denied") {
      const note = document.createElement("p");
      note.className = "role";
      note.textContent = "Denied. The research guide was not changed.";
      card.appendChild(note);
      return;
    }
    const actions = document.createElement("div");
    actions.className = "actions";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.dataset.riskAction = "apply";
    apply.dataset.riskId = row.id || "";
    apply.textContent = "Apply";
    const deny = document.createElement("button");
    deny.type = "button";
    deny.className = "deny";
    deny.dataset.riskAction = "deny";
    deny.dataset.riskId = row.id || "";
    deny.textContent = "Deny";
    actions.append(apply, deny);
    card.appendChild(actions);
  });
}

document.querySelector("#risk-suggestions")?.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-risk-action]");
  if (!button) return;
  const action = button.dataset.riskAction;
  button.disabled = true;
  const response = await fetch(`/api/risk-suggestions/${encodeURIComponent(button.dataset.riskId)}/${action}`, { method: "POST" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    button.disabled = false;
    button.textContent = payload.detail || "Not saved";
    return;
  }
  const host = document.querySelector("#risk-suggestions");
  if (host) delete host.dataset.at;
  const guide = document.querySelector("#research-guide");
  if (guide) delete guide.dataset.at;
  loadRiskSuggestions();
});

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
  const trades = state.book.trades || [];
  document.querySelector("#trades tbody").innerHTML = trades.map(() => `<tr><td></td><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>`).join("")
    || `<tr><td colspan="8">No closed trade yet.</td></tr>`;
  document.querySelectorAll("#trades tbody tr").forEach((node, index) => {
    const row = trades[index];
    if (!row) return;
    const cells = node.querySelectorAll("td");
    [row.time, row.account, row.instrument, row.side, row.qty, row.entry, row.exit, row.pnl].forEach((value, i) => {
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
    engines: (state.lab.engines || []).map((engine) => `${engine.id}:${engine.busy}:${engine.studying ? 1 : 0}:${engine.successes || 0}:${(engine.results || []).length}:${engine.study || ""}:${(engine.logs || []).at?.(-1) || ""}`).join("|"),
    progress: ((state.lab.progress && state.lab.progress.rows) || []).map((row) => `${row.timeframe}:${row.learning_done}/${row.learning_total}:${row.created_done}/${row.created_total}`).join(","),
    orders: (state.book.orders || []).length,
    positions: (state.book.positions || []).length,
    lead: `${state.book.lead?.title || ""}:${state.book.lead?.net_profit ?? ""}:${state.book.lead?.timeframe || ""}`,
    leaders: (state.book.leaders || []).map((item) => `${item.id}:${item.net_profit}`).join(","),
    running: (state.book.running || []).map((item) => `${item.id}:${item.account}`).join(","),
    paper: (state.book.accounts || []).map((row) => `${row.account}:${row.pnl}:${row.equity}:${row.drawdown}`).join("|"),
    strategy: (state.book.strategy?.strategies || []).map((item) => `${item.id}:${item.active}`).join(","),
    intervals: state.book.interval_count,
    mode: state.book.trading_mode,
    executing: (state.book.executing || []).map((slot) => `${slot.id}:${slot.trader}:${slot.feed}:${slot.last_bar}:${slot.note}:${slot.trades}:${slot.position?.pnl ?? ""}:${slot.position?.stop ?? ""}:${slot.engine?.runs ?? ""}:${slot.engine?.pid ?? ""}`).join("|"),
    cards: (state.book.active_strategies || []).map((row) => `${row.id}:${row.locked}:${row.title}:${(row.text || "").length}`).join(","),
    acceptance: `${state.book.strategy_acceptance?.goal || ""}:${state.book.strategy_acceptance?.min_profit ?? ""}:${state.book.strategy_acceptance?.profit_factor ?? ""}:${state.book.strategy_acceptance?.win_rate ?? ""}:${state.book.strategy_acceptance?.min_trades ?? ""}:${state.book.strategy_acceptance?.drawdown_pct ?? ""}:${state.book.strategy_acceptance?.use_drawdown}:${state.book.strategy_acceptance?.use_trades}:${state.book.strategy_acceptance?.use_profit}:${state.book.strategy_acceptance?.use_factor}:${state.book.strategy_acceptance?.use_win_rate}`,
  });
}

let refreshing = null;
let refreshAgain = false;

async function refresh() {
  if (refreshing) {
    refreshAgain = true;
    return refreshing;
  }
  refreshing = (async () => {
    try {
      do {
        refreshAgain = false;
        await refreshOnce();
      } while (refreshAgain);
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

async function refreshOnce() {
  const libraryTask = state.channel === "library" ? ensureLibrary() : null;
  const seenActive = activeEpoch;
  const [roster, snapshot, book, lab] = await Promise.all([
    fetch("/api/roster").then((response) => response.json()),
    fetch("/api/state").then((response) => response.json()),
    fetch("/api/book").then((response) => response.json()),
    fetch("/api/lab").then((response) => response.json()),
  ]);
  state.roster = roster.agents || [];
  state.snapshot = snapshot;
  if (seenActive !== activeEpoch) book.active_strategies = state.book.active_strategies;
  state.book = book;
  state.lab = lab;
  paintKronosButton();
  ensureKronosPaper();
  if (state.channel === "chart") ensureChart();
  if (state.channel === "engine") loadChecks();
  if (state.channel === "indicators") ensureIndicators();
  if (libraryTask) await libraryTask;
  const key = paintKey();
  if (key === state.paint) return;
  state.paint = key;
  renderChannels();
  renderFeed();
  renderBook();
  renderLab();
}

function shownAccounts() {
  const rows = state.book.accounts?.length ? state.book.accounts : [];
  const names = ["paper", "paper-2"];
  const paper = names.map((name) => rows.find((row) => row.account === name)).filter(Boolean);
  return paper.length ? paper : rows;
}

function accountLabel(name) {
  return name === "paper" || name === "paper-2" ? "paper" : (name || "Account");
}

function renderAccounts() {
  const box = document.querySelector("#account-metrics");
  if (!box) return;
  const rows = shownAccounts();
  const positions = state.book.positions || [];
  const stamp = `${rows.map((row) => `${row.account}:${row.pnl}:${row.equity}:${row.drawdown}`).join("|")}|${positions.map((row) => `${row.account}:${row.instrument}:${row.contracts}:${row.average_price}:${row.pnl}:${row.strategy || ""}`).join("|")}`;
  if (box.dataset.stamp === stamp && box.childElementCount === rows.length) return;
  box.dataset.stamp = stamp;
  box.innerHTML = rows.map(() => `
    <article class="account-card">
      <p class="kicker account-name"></p>
      <div class="account-figures">
        <div><p class="kicker">P&L</p><strong></strong></div>
        <div><p class="kicker">Drawdown</p><strong></strong></div>
        <div><p class="kicker">Equity</p><strong></strong></div>
      </div>
      <p class="open-trade"></p>
    </article>
  `).join("");
  box.querySelectorAll(".account-card").forEach((card, index) => {
    const row = rows[index];
    if (!row) return;
    card.querySelector(".account-name").textContent = accountLabel(row.account);
    const figures = card.querySelectorAll("strong");
    figures[0].textContent = money(row.pnl ?? row.active_pnl);
    figures[1].textContent = money(row.drawdown);
    figures[2].textContent = money(row.equity ?? row.size ?? 50000);
    const trades = positions.filter((position) => position.account === row.account);
    const line = card.querySelector(".open-trade");
    line.textContent = trades.length
      ? trades.map((position) => {
          const qty = Math.abs(Number(position.contracts) || 0);
          const side = Number(position.contracts) < 0 ? "Short" : "Long";
          const who = position.strategy ? ` · ${position.strategy}, ${position.trader}` : "";
          return `${side} ${qty} ${position.instrument} from ${position.average_price}, open ${money(position.pnl ?? 0)}${who}`;
        }).join(" · ")
      : "No open trade";
  });
}

function fillChatAgents() {
  const select = document.querySelector("#chat-agent");
  if (!select || select.dataset.ready === "1" || !state.roster.length) return;
  select.innerHTML = state.roster.map((agent) => `<option value="${escapeHtml(agent.name)}">${escapeHtml(agent.name)}</option>`).join("");
  if ([...select.options].some((option) => option.value === "Portfolio Manager")) select.value = "Portfolio Manager";
  select.dataset.ready = "1";
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
    <span class="chart-tab-wrap">
      <button type="button" class="chart-tab ${chart.id === state.chartId ? "active" : ""}" data-chart="${escapeHtml(chart.id)}">${escapeHtml(chart.contract)} ${escapeHtml(chart.timeframe)}</button>
      ${state.charts.length > 1 ? `<button type="button" class="chart-tab-close" data-close="${escapeHtml(chart.id)}" aria-label="Remove ${escapeHtml(chart.contract)} chart">×</button>` : ""}
    </span>
  `).join("");
  const select = document.querySelector("#chart-timeframe");
  const chart = state.charts.find((item) => item.id === state.chartId) || state.charts[0];
  if (select && chart && document.activeElement !== select) select.value = chart.timeframe;
}

let chartToken = 0;
let chartAt = 0;
let chartBusy = false;
let chartFlightKey = "";
let chartDraw = 0;

async function ensureChart() {
  renderChartTabs();
  const chart = state.charts.find((item) => item.id === state.chartId) || state.charts[0];
  if (!chart) return;
  const key = `${chart.contract}|${chart.timeframe}`;
  const same = state.chartData && state.chartData.contract === chart.contract && state.chartData.timeframe === chart.timeframe;
  if (!same) paintChartWaiting();
  if (same) scheduleChartDraw();
  if (chartBusy && chartFlightKey === key) return;
  if (same && Date.now() - chartAt < 8000) return;
  const token = ++chartToken;
  chartBusy = true;
  chartFlightKey = key;
  const query = `contract=${encodeURIComponent(chart.contract)}&timeframe=${encodeURIComponent(chart.timeframe)}`;
  try {
    const response = await fetch(`/api/chart?${query}&limit=1200`);
    const payload = await response.json().catch(() => ({}));
    if (token !== chartToken) return;
    if (!response.ok) {
      document.querySelector("#chart-readout").textContent = payload.detail || "The chart did not load.";
      return;
    }
    state.chartData = payload;
    chartAt = Date.now();
    paintKronosAccuracy(payload);
    scheduleChartDraw();
  } catch (error) {
    if (token !== chartToken) return;
    const readout = document.querySelector("#chart-readout");
    if (readout && !state.chartData) readout.textContent = "The chart did not load.";
  } finally {
    if (token === chartToken) chartBusy = false;
  }
}

function scheduleChartDraw() {
  const ticket = ++chartDraw;
  requestAnimationFrame(() => {
    if (ticket !== chartDraw || state.channel !== "chart") return;
    fitChartCanvas();
    if (state.chartData) drawCandles(state.chartData);
    else paintChartWaiting();
  });
}

function paintChartWaiting() {
  const canvas = document.querySelector("#chart-canvas");
  if (!canvas) return;
  fitChartCanvas();
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#0d1117";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#d5dde6";
  context.font = "16px Segoe UI, sans-serif";
  context.fillText("Loading candles…", 24, 40);
}

function fitChartCanvas() {
  const canvas = document.querySelector("#chart-canvas");
  const stage = document.querySelector(".chart-stage");
  const book = document.querySelector("#chart");
  if (!canvas || !stage || !book || book.hidden) return;
  const width = Math.max(320, Math.floor(stage.clientWidth || book.clientWidth || 320));
  let height = Math.floor(stage.clientHeight);
  if (height < 180) {
    const top = stage.getBoundingClientRect().top;
    const limit = Math.min(window.innerHeight - 28, book.getBoundingClientRect().bottom);
    height = Math.max(220, Math.floor(limit - top));
  }
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
}

function chartIndicators() {
  const chart = state.charts.find((item) => item.id === state.chartId) || state.charts[0];
  if (!chart) return { volume: true, macd: true, delta: true };
  if (!chart.indicators) chart.indicators = { volume: true, macd: true, delta: true };
  document.querySelectorAll("#chart-indicators input").forEach((box) => {
    const name = box.dataset.indicator;
    const available = (state.chartData?.indicators || []).includes(name);
    box.disabled = !available;
    box.checked = available && chart.indicators[name] !== false;
  });
  return chart.indicators;
}

function barMillis(timeframe) {
  const match = String(timeframe || "1m").match(/^(\d+)(m|h)$/);
  if (!match) return 60 * 1000;
  const count = Number(match[1]);
  return match[2] === "h" ? count * 60 * 60 * 1000 : count * 60 * 1000;
}

function gapLabel(minutes) {
  const whole = Math.max(1, Math.round(minutes));
  if (whole >= 60) {
    const hours = Math.floor(whole / 60);
    const rest = whole % 60;
    return rest ? `${hours}h ${rest}m` : `${hours}h`;
  }
  return `${whole}m`;
}

function withGaps(candles, timeframe) {
  const width = barMillis(timeframe);
  const items = [];
  let previous = null;
  candles.forEach((candle) => {
    const epoch = Date.parse(candle.t);
    if (previous != null && Number.isFinite(epoch) && epoch - previous > width * 1.5) {
      items.push({ kind: "gap", minutes: (epoch - previous) / 60000 });
    }
    items.push({ kind: "candle", candle });
    if (Number.isFinite(epoch)) previous = epoch;
  });
  if (previous != null && Date.now() - previous > width * 1.5) {
    items.push({ kind: "gap", minutes: (Date.now() - previous) / 60000 });
  }
  return items;
}

function chartCandles(payload) {
  const printed = payload.candles || [];
  const forecast = ((payload.prediction || {}).candles || []).map((candle) => ({ ...candle, predicted: true }));
  return printed.concat(forecast);
}

function candleDash(candle) {
  return candle && candle.predicted ? [5, 4] : [];
}

function drawCandles(payload) {
  const canvas = document.querySelector("#chart-canvas");
  const readout = document.querySelector("#chart-readout");
  if (!canvas || !payload) return;
  try {
    drawCandlePlot(payload, canvas, readout);
  } catch (error) {
    const context = canvas.getContext("2d");
    context.fillStyle = "#0d1117";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#d5dde6";
    context.font = "16px Segoe UI, sans-serif";
    context.fillText("The chart could not be drawn.", 24, 40);
    if (readout) readout.textContent = "The chart could not be drawn.";
  }
}

function drawCandlePlot(payload, canvas, readout) {
  const all = withGaps(chartCandles(payload), payload.timeframe);
  const chart = state.charts.find((item) => item.id === state.chartId) || state.charts[0];
  const visible = visibleCount(chart);
  let start = 0;
  if (chart && all.length > visible) {
    if (chart.follow !== false) start = all.length - visible;
    else start = Math.min(chart.start || 0, all.length - visible);
    chart.start = start;
  } else if (chart) {
    chart.start = 0;
  }
  syncChartScroll(all.length, visible, start);
  const candles = all.slice(start, start + visible);
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#0d1117";
  context.fillRect(0, 0, width, height);
  if (!candles.length) {
    context.fillStyle = "#ddd";
    context.font = "16px Segoe UI, sans-serif";
    context.fillText(payload.note || "No candles.", 24, 40);
    if (readout) readout.textContent = payload.note || "";
    return;
  }
  const selected = chartIndicators();
  const panes = ["price"];
  if (selected.volume && (payload.indicators || []).includes("volume")) panes.push("volume");
  if (selected.macd && (payload.indicators || []).includes("macd")) panes.push("macd");
  if (selected.delta && (payload.indicators || []).includes("delta")) panes.push("delta");
  const pad = { top: 12, right: 78, bottom: 22, left: 8 };
  const plotW = width - pad.left - pad.right;
  const slot = plotW / candles.length;
  const weights = { price: 5, volume: 1.3, macd: 1.6, delta: 1.3 };
  const total = panes.reduce((sum, name) => sum + weights[name], 0);
  const usable = height - pad.top - pad.bottom - (panes.length - 1) * 8;
  let top = pad.top;
  const boxes = {};
  panes.forEach((name) => {
    const paneHeight = (usable * weights[name]) / total;
    boxes[name] = { top, height: paneHeight };
    top += paneHeight + 8;
  });
  context.strokeStyle = "#1e2a3a";
  context.fillStyle = "#8b98a5";
  context.font = "11px Segoe UI, sans-serif";
  drawPrice(context, candles, payload.orders || [], boxes.price, slot, pad, width);
  if (boxes.volume) drawHistogram(context, candles, boxes.volume, slot, pad, width, "v", "Volume");
  if (boxes.macd) drawMacd(context, candles, boxes.macd, slot, pad, width);
  if (boxes.delta) drawHistogram(context, candles, boxes.delta, slot, pad, width, "delta", "Volume delta");
  const latest = [...all].reverse().find((item) => item.kind === "candle" && !(item.candle && item.candle.predicted));
  const first = candles.find((item) => item.kind === "candle");
  const lastDrawn = [...candles].reverse().find((item) => item.kind === "candle");
  context.fillStyle = "#8b98a5";
  if (first) context.fillText(first.candle.et, pad.left, height - 6);
  if (lastDrawn) context.fillText(lastDrawn.candle.et, width - 180, height - 6);
  state.chartSlot = (width - pad.left - pad.right) / candles.length;
  if (readout && latest) {
    const live = latest.candle.forming ? "  forming" : "";
    const hole = [...candles].reverse().find((item) => item.kind === "gap");
    const missing = hole ? `  no prices for ${gapLabel(hole.minutes)}` : "";
    const forecast = (payload.prediction && payload.prediction.note) ? `  ${payload.prediction.note}` : "";
    readout.textContent = `${payload.contract} ${payload.timeframe}${live}  ${latest.candle.et} ET${candleFacts(latest.candle)}${missing}${forecast}`;
  }
}

function paintKronosAccuracy(payload) {
  const accuracy = document.querySelector("#kronos-accuracy");
  const match = payload && payload.direction_match;
  if (!accuracy || !match || !match.text) return;
  accuracy.textContent = match.text;
}

function kronosFollowView() {
  const executing = (state.book && state.book.executing) || [];
  const kronos = executing.find((slot) => slot.kind === "kronos" || slot.id === "kronos-paper");
  const taken = new Set(executing.filter((slot) => slot !== kronos).map((slot) => slot.trader));
  const free = ["Floor Trader", "Quantitative Trader"].find((name) => !taken.has(name)) || "";
  const chart = state.charts.find((item) => item.id === state.chartId) || state.charts[0] || { contract: "ES1!", timeframe: "5m" };
  return { kronos, trader: (kronos && kronos.trader) || free, chart };
}

let kronosArmed = true;
let kronosBusy = false;

function paintKronosButton() {
  const view = kronosFollowView();
  document.querySelectorAll("[data-kronos-follow]").forEach((button) => {
    if (button.dataset.busy === "1") return;
    if (view.kronos) {
      const where = `${view.kronos.contract} ${view.kronos.timeframe}`;
      button.disabled = false;
      button.textContent = `Stop ${view.trader}'s Kronos paper trades on ${where}`;
      return;
    }
    if (!view.trader) {
      button.disabled = true;
      button.textContent = "Both traders are executing a strategy";
      return;
    }
    button.disabled = false;
    button.textContent = `Have the ${view.trader} paper trade Kronos on ${view.chart.contract} ${view.chart.timeframe}`;
  });
}

async function ensureKronosPaper() {
  if (!kronosArmed || kronosBusy) return;
  const view = kronosFollowView();
  if (!view.trader || !view.chart) return;
  const chart = view.chart;
  if (view.kronos) {
    const same = view.kronos.contract === chart.contract && view.kronos.timeframe === chart.timeframe;
    if (same || view.kronos.position) return;
  }
  kronosBusy = true;
  try {
    if (view.kronos) {
      const stopped = await postKronos("/api/execution/kronos-paper/stop");
      if (!stopped) return;
    }
    const started = await postKronos("/api/kronos-paper/start", { contract: chart.contract, timeframe: chart.timeframe });
    if (!started) {
      const note = document.querySelector("#kronos-follow-note")?.textContent || "";
      if (!/already/i.test(note)) kronosArmed = false;
    }
  } finally {
    kronosBusy = false;
  }
}

async function onKronosFollow(button) {
  const view = kronosFollowView();
  const note = document.querySelector("#kronos-follow-note");
  if (view.kronos) {
    const open = view.kronos.position ? " The open paper trade is closed first." : "";
    if (!window.confirm(`Stop ${view.trader}'s Kronos paper trades on ${view.kronos.contract} ${view.kronos.timeframe}?${open}`)) return;
    kronosArmed = false;
    button.dataset.busy = "1";
    button.disabled = true;
    await postKronos("/api/execution/kronos-paper/stop");
    delete button.dataset.busy;
    paintKronosButton();
    return;
  }
  if (!view.trader) return;
  const chart = view.chart;
  if (!window.confirm(`Have the ${view.trader} paper trade Kronos buy and sell signals on ${chart.contract} ${chart.timeframe}? One contract, paper only. No order is sent to the live account.`)) return;
  kronosArmed = true;
  button.dataset.busy = "1";
  button.disabled = true;
  const payload = await postKronos("/api/kronos-paper/start", { contract: chart.contract, timeframe: chart.timeframe });
  delete button.dataset.busy;
  paintKronosButton();
  if (note && payload && payload.note) note.textContent = payload.note;
}

async function postKronos(path, body) {
  const note = document.querySelector("#kronos-follow-note");
  const active = document.querySelector("#active-note");
  if (note) note.textContent = "Asking the desk…";
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (note) note.textContent = "The desk did not answer.";
    return null;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || "The desk refused.";
    if (note) note.textContent = detail;
    if (active) active.textContent = detail;
    return null;
  }
  if (note) note.textContent = payload.note || "";
  if (active) active.textContent = payload.note || "";
  const list = document.querySelector("#active-list");
  if (list) delete list.dataset.stamp;
  state.paint = "";
  refresh();
  return payload;
}

function visibleCount(chart) {
  const count = Number(chart && chart.visible) || 140;
  return Math.max(24, Math.min(800, count));
}

function candleFacts(candle) {
  const parts = [
    `O ${candle.o}`,
    `H ${candle.h}`,
    `L ${candle.l}`,
    `C ${candle.c}`,
    `V ${candle.v}`,
  ];
  const extra = [
    ["MACD", candle.macd],
    ["Signal", candle.macd_signal],
    ["Hist", candle.macd_histogram],
    ["Delta", candle.delta],
    ["Delta %", candle.delta_pct],
    ["POC", candle.poc],
  ];
  extra.forEach(([label, value]) => {
    if (value == null || value === "") return;
    const number = Number(value);
    if (!Number.isFinite(number)) return;
    parts.push(`${label} ${number.toFixed(2)}`);
  });
  return `  ${parts.join("  ")}`;
}

function zoomChart(direction) {
  const chart = state.charts.find((item) => item.id === state.chartId);
  if (!chart || !state.chartData) return;
  const current = visibleCount(chart);
  const next = direction < 0 ? Math.max(24, Math.round(current * 0.7)) : Math.min(800, Math.round(current * 1.4));
  const end = (chart.start || 0) + current;
  chart.visible = next;
  chart.start = Math.max(0, end - next);
  const total = chartCandles(state.chartData).length;
  chart.follow = chart.start >= Math.max(0, total - next);
  drawCandles(state.chartData);
}

function syncChartScroll(count, visible, start) {
  const bar = document.querySelector("#chart-scroll");
  if (!bar) return;
  const max = Math.max(0, count - visible);
  bar.max = String(max);
  bar.value = String(Math.min(start, max));
  bar.disabled = max === 0;
}

function drawGap(context, index, slot, pad, box, minutes) {
  const x = pad.left + index * slot;
  context.fillStyle = "rgba(139, 152, 165, 0.16)";
  context.fillRect(x + 1, box.top, Math.max(2, slot - 2), box.height);
  context.fillStyle = "#c5d0dc";
  context.fillText(gapLabel(minutes), x + 3, box.top + 14);
}

function drawPrice(context, candles, orders, box, slot, pad, width) {
  const priced = candles.filter((item) => item.kind === "candle").map((item) => item.candle);
  if (!priced.length) return;
  const min = Math.min(...priced.map((candle) => candle.l));
  const max = Math.max(...priced.map((candle) => candle.h));
  const span = max - min || 1;
  const y = (price) => box.top + ((max - price) / span) * box.height;
  for (let step = 0; step <= 4; step += 1) {
    const price = min + (span * step) / 4;
    const yy = y(price);
    context.beginPath();
    context.moveTo(pad.left, yy);
    context.lineTo(width - pad.right, yy);
    context.stroke();
    context.fillText(price.toFixed(2), width - pad.right + 6, yy + 4);
  }
  candles.forEach((item, index) => {
    if (item.kind === "gap") {
      drawGap(context, index, slot, pad, box, item.minutes);
      return;
    }
    const candle = item.candle;
    const x = pad.left + index * slot + slot / 2;
    const up = candle.c >= candle.o;
    const bodyTop = y(Math.max(candle.o, candle.c));
    const bodyBottom = y(Math.min(candle.o, candle.c));
    const bodyX = x - Math.max(1, slot * 0.32);
    const bodyW = Math.max(2, slot * 0.64);
    const bodyH = Math.max(1, bodyBottom - bodyTop);
    context.setLineDash(candleDash(candle));
    context.strokeStyle = up ? "#26a69a" : "#ef5350";
    context.beginPath();
    context.moveTo(x, y(candle.h));
    context.lineTo(x, y(candle.l));
    context.stroke();
    if (candle.predicted) {
      context.fillStyle = up ? "rgba(38,166,154,0.18)" : "rgba(239,83,80,0.18)";
      context.fillRect(bodyX, bodyTop, bodyW, bodyH);
      context.strokeRect(bodyX, bodyTop, bodyW, bodyH);
    } else {
      context.fillStyle = context.strokeStyle;
      context.fillRect(bodyX, bodyTop, bodyW, bodyH);
    }
    context.setLineDash([]);
    if (candle.predicted && candle.direction) {
      context.fillStyle = "#d5dde6";
      context.fillText(candle.direction, x + 6, Math.max(box.top + 12, y(candle.h) - 6));
    }
    if (!candle.predicted) {
      orders.filter((order) => order.t === candle.t).forEach((order) => {
        context.fillStyle = order.side === "BUY" ? "#26a69a" : "#ef5350";
        context.beginPath();
        context.arc(x, order.side === "BUY" ? y(candle.l) + 8 : y(candle.h) - 8, 3.5, 0, Math.PI * 2);
        context.fill();
      });
    }
  });
  const printed = priced.filter((candle) => !candle.predicted);
  const last = printed.length ? printed[printed.length - 1] : priced[priced.length - 1];
  context.strokeStyle = "#787b86";
  context.setLineDash([3, 3]);
  context.beginPath();
  context.moveTo(pad.left, y(last.c));
  context.lineTo(width - pad.right, y(last.c));
  context.stroke();
  context.setLineDash([]);
  context.fillStyle = last.c >= last.o ? "#26a69a" : "#ef5350";
  context.fillRect(width - pad.right + 2, y(last.c) - 8, 70, 16);
  context.fillStyle = "#fff";
  context.fillText(Number(last.c).toFixed(2), width - pad.right + 6, y(last.c) + 4);
}

function drawHistogram(context, candles, box, slot, pad, width, key, label) {
  const values = candles.filter((item) => item.kind === "candle" && !item.candle.predicted).map((item) => Number(item.candle[key] || 0));
  if (!values.length) return;
  const peak = Math.max(...values.map((value) => Math.abs(value)), 1);
  const signed = values.some((value) => value < 0);
  const mid = box.top + box.height / 2;
  context.fillStyle = "#8b98a5";
  context.fillText(label, pad.left, box.top + 11);
  candles.forEach((item, index) => {
    if (item.kind === "gap") {
      drawGap(context, index, slot, pad, box, item.minutes);
      return;
    }
    if (item.candle.predicted) return;
    const value = Number(item.candle[key] || 0);
    const x = pad.left + index * slot + slot * 0.2;
    const wide = Math.max(1, slot * 0.6);
    context.fillStyle = value >= 0 ? "#26a69a" : "#ef5350";
    if (!signed) {
      const barHeight = (value / peak) * (box.height - 16);
      context.fillRect(x, box.top + box.height - barHeight, wide, barHeight);
      return;
    }
    const barHeight = (Math.abs(value) / peak) * (box.height / 2 - 4);
    context.fillRect(x, value >= 0 ? mid - barHeight : mid, wide, barHeight);
  });
}

function drawMacd(context, candles, box, slot, pad, width) {
  const values = candles.filter((item) => item.kind === "candle" && !item.candle.predicted).flatMap((item) => [Number(item.candle.macd || 0), Number(item.candle.macd_signal || 0), Number(item.candle.macd_histogram || 0)]);
  if (!values.length) return;
  const peak = Math.max(...values.map((value) => Math.abs(value)), 1);
  const y = (value) => box.top + box.height / 2 - (value / peak) * (box.height / 2 - 4);
  context.fillStyle = "#8b98a5";
  context.fillText("MACD 12 26 9", pad.left, box.top + 11);
  candles.forEach((item, index) => {
    if (item.kind === "gap") {
      drawGap(context, index, slot, pad, box, item.minutes);
      return;
    }
    if (item.candle.predicted) return;
    const hist = Number(item.candle.macd_histogram || 0);
    const barHeight = Math.abs(y(hist) - y(0));
    const x = pad.left + index * slot + slot * 0.25;
    context.fillStyle = hist >= 0 ? "rgba(38,166,154,0.7)" : "rgba(239,83,80,0.7)";
    context.fillRect(x, Math.min(y(hist), y(0)), Math.max(1, slot * 0.5), barHeight);
  });
  drawLine(context, candles, slot, pad, y, "macd", "#2962ff");
  drawLine(context, candles, slot, pad, y, "macd_signal", "#ff6d00");
}

function drawLine(context, candles, slot, pad, y, key, color) {
  context.strokeStyle = color;
  context.beginPath();
  let started = false;
  candles.forEach((item, index) => {
    if (item.kind === "gap" || item.candle[key] == null) {
      started = false;
      return;
    }
    const x = pad.left + index * slot + slot / 2;
    const point = y(Number(item.candle[key]));
    if (!started) {
      context.moveTo(x, point);
      started = true;
    } else {
      context.lineTo(x, point);
    }
  });
  context.stroke();
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

function acceptanceStamp(rules) {
  return [
    rules.min_profit, rules.profit_factor, rules.win_rate, rules.min_trades,
    rules.drawdown_pct, rules.goal || "min_trades",
    rules.use_drawdown, rules.use_trades, rules.use_profit, rules.use_factor, rules.use_win_rate,
  ].join("|");
}

function renderAcceptance() {
  const form = document.querySelector("#acceptance-rules");
  if (!form) return;
  const rules = state.book.strategy_acceptance;
  if (!rules || form.dataset.editing === "1") return;
  const stamp = acceptanceStamp(rules);
  if (form.dataset.stamp === stamp) return;
  form.dataset.stamp = stamp;
  form.min_profit.value = rules.min_profit ?? 20000;
  form.profit_factor.value = rules.profit_factor ?? 1.1;
  form.win_rate.value = rules.win_rate ?? 53;
  form.min_trades.value = rules.min_trades ?? 100;
  form.drawdown_pct.value = rules.drawdown_pct ?? 5;
  form.goal.value = rules.goal || "min_trades";
  form.use_drawdown.checked = rules.use_drawdown !== false;
  form.use_trades.checked = rules.use_trades !== false;
  form.use_profit.checked = rules.use_profit === true;
  form.use_factor.checked = rules.use_factor === true;
  form.use_win_rate.checked = rules.use_win_rate === true;
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
      <p>${escapeHtml(row.timeframe || "")} · profit ${money(row.net_profit)} · drawdown ${money(row.max_drawdown)} · ${row.trades ?? 0} trades${row.trades_per_day == null ? "" : ` · ${row.trades_per_day} a day`}</p>
      <p>${escapeHtml(row.charts || "")}</p>
      <label>Account <input data-account value="${escapeHtml(account)}" placeholder="Account name" maxlength="64" ${live ? "readonly" : ""} /></label>
      <button type="button" data-run="${live ? "stop" : "start"}">${live ? "Stop" : "Run"}</button>
      <pre class="alert-msg">${escapeHtml(strategyAlert(row.title, account))}</pre>
    </article>`;
  }).join("") || "<p>No strategy has finished green on two charts yet.</p>";
}

function setScriptMode(mode) {
  const modal = document.querySelector("#script-modal");
  const form = document.querySelector("#script-add-form");
  const body = document.querySelector("#script-body");
  modal.dataset.mode = mode;
  form.hidden = mode !== "add";
  document.querySelector("#script-save").hidden = mode !== "add";
  document.querySelector("#script-copy").hidden = mode === "add";
  document.querySelector("#script-delete").hidden = mode !== "library";
  body.readOnly = mode !== "add";
}

async function openStrategy(id, source, contract) {
  const modal = document.querySelector("#script-modal");
  const title = document.querySelector("#script-title");
  const body = document.querySelector("#script-body");
  const note = document.querySelector("#script-note");
  setScriptMode("view");
  modal.hidden = false;
  modal.dataset.loading = "1";
  modal.dataset.strategy = id || "";
  modal.dataset.contract = contract || "";
  title.textContent = "Strategy";
  body.value = "";
  note.textContent = "Loading the script.";
  const path = source === "library" ? "/api/library/" : source === "indicators" ? "/api/indicators/" : "/api/leaders/";
  const query = source === "library" && contract ? `?contract=${encodeURIComponent(contract)}` : "";
  const response = await fetch(`${path}${encodeURIComponent(id)}${query}`);
  const payload = await response.json().catch(() => ({}));
  delete modal.dataset.loading;
  if (!response.ok || !payload.pine) {
    note.textContent = payload.detail || "That script could not be opened.";
    return;
  }
  title.textContent = payload.title || "Strategy";
  body.value = payload.pine;
  note.textContent = "";
  if (source === "library") setScriptMode("library");
}

function openLibraryAdd() {
  const modal = document.querySelector("#script-modal");
  setScriptMode("add");
  modal.hidden = false;
  delete modal.dataset.loading;
  modal.dataset.strategy = "";
  modal.dataset.contract = "";
  document.querySelector("#script-title").textContent = "Add a strategy";
  document.querySelector("#script-body").value = "";
  document.querySelector("#script-add-title").value = "";
  document.querySelector("#script-add-timeframe").value = "";
  document.querySelector("#script-note").textContent = "Paste a Pine strategy. It stays on the shelf and on the one-year list until you delete it.";
  document.querySelector("#script-add-title").focus();
}

async function saveLibraryStrategy() {
  const note = document.querySelector("#script-note");
  const title = document.querySelector("#script-add-title").value.trim();
  const pine = document.querySelector("#script-body").value;
  if (!title || !pine.includes("strategy(")) {
    note.textContent = "Give it a name and a script that contains strategy().";
    return;
  }
  const response = await fetch("/api/library", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title,
      contract: document.querySelector("#script-add-contract").value,
      timeframe: document.querySelector("#script-add-timeframe").value.trim(),
      pine,
    }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    note.textContent = payload.detail || "The strategy was not added.";
    return;
  }
  closeStrategy();
  await ensureLibrary();
}

async function deleteLibrary(id, contract) {
  if (!id) return;
  const response = await fetch(`/api/library/${encodeURIComponent(id)}?contract=${encodeURIComponent(contract || "")}`, { method: "DELETE" });
  if (!response.ok) {
    const note = document.querySelector("#script-note");
    const payload = await response.json().catch(() => ({}));
    if (note && !document.querySelector("#script-modal").hidden) {
      note.textContent = payload.detail || "That strategy was not removed.";
    }
    return;
  }
  closeStrategy();
  await ensureLibrary();
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
document.querySelector("#library-add").addEventListener("click", openLibraryAdd);
document.querySelector("#script-add").addEventListener("click", openLibraryAdd);
document.querySelector("#script-save").addEventListener("click", saveLibraryStrategy);
document.querySelector("#script-delete").addEventListener("click", () => {
  const modal = document.querySelector("#script-modal");
  deleteLibrary(modal.dataset.strategy, modal.dataset.contract);
});
document.querySelector("#script-copy").addEventListener("click", async () => {
  const text = document.querySelector("#script-body").value;
  const note = document.querySelector("#script-note");
  if (document.querySelector("#script-modal").dataset.loading) {
    note.textContent = "The script is still loading.";
    return;
  }
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

let indicatorsFetched = 0;
let indicatorRows = [];

async function ensureIndicators() {
  if (Date.now() - indicatorsFetched < 10000) return;
  indicatorsFetched = Date.now();
  try {
    const payload = await (await fetch("/api/indicators")).json();
    indicatorRows = payload.indicators || [];
  } catch (error) {
    return;
  }
  renderIndicators();
}

function renderIndicators() {
  const body = document.querySelector("#indicators-table tbody");
  if (!body) return;
  const query = (document.querySelector("#indicators-search")?.value || "").trim().toLowerCase();
  const kind = document.querySelector("#indicators-kind")?.value || "all";
  const rows = indicatorRows.filter((row) => {
    if (kind !== "all" && (kind === "built-in") !== (row.kind === "built-in")) return false;
    if (!query) return true;
    return `${row.name} ${row.summary || ""} ${(row.scripts || []).join(" ")}`.toLowerCase().includes(query);
  });
  body.innerHTML = rows.map((row) => {
    const scripts = row.scripts || [];
    const used = scripts.length ? `${scripts.length}: ${escapeHtml(scripts.slice(0, 3).join(", "))}${scripts.length > 3 ? ", …" : ""}` : "Not used yet";
    return `<tr data-indicator="${escapeHtml(row.id)}">
      <td>${escapeHtml(row.name)}</td>
      <td>${escapeHtml(row.kind)}</td>
      <td>${escapeHtml(row.summary || "")}</td>
      <td>${used}</td>
      <td>${row.engine ? "Yes" : "Not yet"}</td>
      <td>${escapeHtml(row.added_by || "")}${row.added_at ? ` · ${escapeHtml(row.added_at)}` : ""}</td>
    </tr>`;
  }).join("");
  const empty = document.querySelector("#indicators-empty");
  if (empty) empty.textContent = rows.length ? `${rows.length} of ${indicatorRows.length} indicators.` : "No indicator matches.";
}

async function ensureLibrary() {
  const response = await fetch("/api/library?contract=all");
  const payload = await response.json().catch(() => ({}));
  state.libraryRows = payload.strategies || [];
  state.untestedRows = payload.untested || [];
  state.libraryContracts = payload.contracts || [];
  const select = document.querySelector("#library-contract");
  if (select) {
    const shelves = payload.contracts || [];
    const current = select.value;
    select.innerHTML = shelves.map((item) => `<option value="${escapeHtml(item.contract)}">${escapeHtml(item.label)} (${item.count})</option>`).join("");
    select.value = shelves.some((item) => item.contract === current) ? current : (shelves.some((item) => item.contract === "ES1!") ? "ES1!" : (shelves[0]?.contract || "ES1!"));
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
  const textKeys = new Set(["title", "timeframe", "contract_label", "kind", "added_at"]);
  rows.sort((left, right) => {
    const a = left[key];
    const b = right[key];
    const cmp = textKeys.has(key)
      ? String(a ?? "").localeCompare(String(b ?? ""))
      : Number(a ?? 0) - Number(b ?? 0);
    return dir === "asc" ? cmp : -cmp;
  });
  body.innerHTML = rows.map(libraryRow).join("");
  if (empty) {
    const label = document.querySelector("#library-contract")?.selectedOptions?.[0]?.textContent || "this contract";
    empty.textContent = rows.length ? "" : `No strategy trading at least 4 times a week is filed under ${label}.`;
  }
  renderUntested();
}

function renderUntested() {
  const body = document.querySelector("#untested-table tbody");
  const empty = document.querySelector("#untested-empty");
  if (!body) return;
  const contract = document.querySelector("#library-contract")?.value || "ES1!";
  const query = (document.querySelector("#library-search")?.value || "").trim().toLowerCase();
  let rows = (state.untestedRows || []).filter((row) => row.contract === contract);
  if (query) {
    rows = rows.filter((row) => row.title.toLowerCase().includes(query) || String(row.timeframe || "").toLowerCase().includes(query));
  }
  const { key, dir } = state.untestedSort;
  const textKeys = new Set(["title", "timeframe", "contract_label", "kind", "added_at"]);
  const counts = document.querySelector("#untested-counts");
  if (counts) {
    counts.textContent = (state.libraryContracts || []).map((item) => `${item.label} ${item.untested || 0}`).join(" · ");
  }
  rows.sort((left, right) => {
    const a = left[key];
    const b = right[key];
    const cmp = textKeys.has(key)
      ? String(a ?? "").localeCompare(String(b ?? ""))
      : Number(a ?? 0) - Number(b ?? 0);
    return dir === "asc" ? cmp : -cmp;
  });
  body.innerHTML = rows.map(libraryRow).join("");
  if (empty) {
    const label = document.querySelector("#library-contract")?.selectedOptions?.[0]?.textContent || "this contract";
    empty.textContent = rows.length ? "" : `No one-year strategy is waiting for the five-year test under ${label}.`;
  }
}

function libraryRow(row) {
  return `<tr data-library="${escapeHtml(row.id)}" data-contract="${escapeHtml(row.contract)}">
    <td>${escapeHtml(row.title)}</td>
    <td>${escapeHtml(row.kind || (row.target_met ? "Target met" : "Under the target"))}</td>
    <td>${escapeHtml(row.contract_label)}</td>
    <td>${escapeHtml(row.timeframe || "")}</td>
    <td>${row.net_profit == null ? "" : money(row.net_profit)}</td>
    <td>${row.max_drawdown == null ? "" : money(row.max_drawdown)}</td>
    <td>${row.trades ?? ""}</td>
    <td>${row.trades_per_day == null ? "" : row.trades_per_day}</td>
    <td>${row.win_rate == null ? "" : `${row.win_rate}%`}</td>
    <td>${row.sharpe == null ? "" : row.sharpe}</td>
    <td>${row.profit_ratio == null ? "" : row.profit_ratio}</td>
    <td>${row.biggest_loser == null ? "" : money(row.biggest_loser)}</td>
    <td>${row.biggest_winner == null ? "" : money(row.biggest_winner)}</td>
    <td>${escapeHtml(row.added_at || "")}</td>
    <td><button type="button" class="row-delete" data-delete="${escapeHtml(row.id)}" data-contract="${escapeHtml(row.contract)}">Delete</button></td>
  </tr>`;
}

const ACTIVE_CONTRACTS = ["ES1!", "NQ1!", "GC1!", "MNQ1!"];
const ACTIVE_FRAMES = ["1m", "2m", "5m", "15m"];
const activeDrafts = new Map();

function cardScript(row) {
  const saved = row?.text || "";
  const drafted = activeDrafts.get(row?.id) || "";
  if (/strategy\s*\(/.test(drafted) && drafted !== saved) return drafted;
  return saved;
}

function armStart(card) {
  const button = card.querySelector("[data-start]");
  if (!button) return;
  const text = card.querySelector("textarea")?.value || "";
  const full = (state.book.executing || []).length >= 2;
  button.disabled = full || !/strategy\s*\(/.test(text);
}

function paintCardHeading(card) {
  const heading = card.querySelector("h3");
  if (!heading) return;
  const text = card.querySelector("textarea")?.value || "";
  const title = (text.match(/strategy\s*\(\s*(?:title\s*=\s*)?"([^"]+)"/) || [])[1];
  if (!title) return;
  const contract = card.querySelector("[data-contract]")?.value || "";
  const frame = card.querySelector("[data-frame]")?.value || "";
  heading.textContent = `${title} · ${contract} ${frame}`;
}

function renderActive() {
  const list = document.querySelector("#active-list");
  if (!list || state.channel !== "active") return;
  renderTradingMode();
  const rows = state.book.active_strategies || [];
  const executing = state.book.executing || [];
  const running = new Map(executing.map((slot) => [slot.id, slot]));
  const stamp = rows.map((row) => `${row.id}:${row.locked}:${row.account}:${row.instrument}:${row.timeframe}:${row.order_instrument}:${row.title}:${cardScript(row).length}:${running.get(row.id)?.trader || ""}:${running.get(row.id)?.feed || ""}`).join("|") + `|${executing.length}`;
  const editing = list.contains(document.activeElement) && document.activeElement.matches("textarea, input");
  if (list.dataset.stamp !== stamp && editing) {
    // Painting the list again would take the box away from the person typing in it.
    list.dataset.held = "1";
  } else if (list.dataset.stamp !== stamp) {
    list.dataset.stamp = stamp;
    const named = state.book.account_rules?.account;
    const accounts = [...new Set(["paper", named, ...(state.book.settings?.accounts || [])].filter(Boolean))];
    const full = executing.length >= 2;
    list.innerHTML = rows.map((row) => {
      const slot = running.get(row.id);
      const fixed = row.locked || slot ? "disabled" : "";
      const contract = row.instrument || "ES1!";
      const frame = row.timeframe || "5m";
      const text = cardScript(row);
      const startable = !slot && !full && /strategy\s*\(/.test(text);
      return `
    <article class="active-card ${slot ? "executing" : ""}" data-id="${escapeHtml(row.id)}">
      <h3>${escapeHtml(row.title || "Strategy")} · ${escapeHtml(contract)} ${escapeHtml(frame)}</h3>
      <div class="row">
        <label>Chart
          <select data-contract ${fixed}>
            ${ACTIVE_CONTRACTS.map((item) => `<option ${item === contract ? "selected" : ""}>${item}</option>`).join("")}
          </select>
        </label>
        <label>Timeframe
          <select data-frame ${fixed}>
            ${ACTIVE_FRAMES.map((item) => `<option ${item === frame ? "selected" : ""}>${item}</option>`).join("")}
          </select>
        </label>
        <label>Account
          <select data-account ${fixed}>
            ${accounts.map((account) => `<option ${account === row.account ? "selected" : ""}>${escapeHtml(account)}</option>`).join("")}
          </select>
        </label>
        <label>Live contract
          <input data-order type="text" placeholder="ES 12-26" value="${escapeHtml(row.order_instrument || "")}" ${slot ? "disabled" : ""} />
        </label>
        <button type="button" data-submit ${row.locked || slot ? "disabled" : ""}>Submit</button>
        ${slot
          ? `<button type="button" data-stop>Stop ${escapeHtml(slot.trader.toLowerCase())}</button>`
          : `<button type="button" data-start ${startable ? "" : "disabled"} title="${full ? "Two strategies are executing. Stop one first." : ""}">Start ${escapeHtml(nextTrader(executing).toLowerCase())}</button>`}
        <button type="button" data-remove ${slot ? "disabled" : ""}>Remove</button>
      </div>
      <p class="exec-status"></p>
      <textarea ${row.locked || slot ? "readonly" : ""}>${escapeHtml(text)}</textarea>
    </article>`;
    }).join("") || "<p>No active strategy. Add one for the floor trader to execute.</p>";
  }
  list.querySelectorAll(".active-card").forEach((card) => {
    const line = card.querySelector(".exec-status");
    if (line) line.innerHTML = executionLine(running.get(card.dataset.id));
  });
}

function nextTrader(executing) {
  return executing.some((slot) => slot.trader === "Floor Trader") ? "Quantitative Trader" : "Floor Trader";
}

function executionLine(slot) {
  if (!slot) return "Not executing.";
  const held = slot.position;
  const where = held ? (held.mode === "live" ? `live on ${escapeHtml(held.account)}` : "on paper") : "";
  const trade = held
    ? `<strong>${held.side === "long" ? "Long" : "Short"} ${held.qty} ${escapeHtml(slot.contract)} from ${held.entry}</strong> ${where} since ${escapeHtml(held.entry_time || "")} ET. Last ${held.last ?? ""}, open P&L ${money(held.pnl ?? 0)}.`
      + `${held.stop != null ? ` Stop ${held.stop}.` : ""}${held.take_profit != null ? ` Target ${held.take_profit}.` : ""}`
    : "<strong>Flat</strong>, waiting for the script's next entry.";
  const record = `${slot.trades} closed trade${slot.trades === 1 ? "" : "s"}, ${slot.wins} won, realized ${money(slot.realized)}.`;
  const bar = slot.last_bar ? ` Newest ${escapeHtml(slot.timeframe)} bar ${escapeHtml(slot.last_bar)} ET.` : "";
  const feed = slot.kind === "kronos" || slot.feed === "kronos"
    ? " Candles come from the desk chart. The fill stays on paper."
    : slot.feed === "ninjatrader"
    ? " Candles come from NinjaTrader. Fills stay on paper so they can be compared with the floor trader."
    : " Candles come from TradingView alerts.";
  const engine = slot.engine?.running
    ? `<br />The ${escapeHtml(slot.trader.toLowerCase())}'s trading engine is process ${slot.engine.pid}. It ran the script ${slot.engine.runs} time${slot.engine.runs === 1 ? "" : "s"}${slot.engine.last_run ? `, last at ${escapeHtml(slot.engine.last_run)} in ${slot.engine.seconds}s` : ""}.`
    : "<br />The trading engine starts on the first check.";
  return `${escapeHtml(slot.trader)} is executing since ${escapeHtml(slot.started)} ET. ${trade} ${record}${bar}${feed}`
    + `${engine}${slot.note ? `<br />${escapeHtml(slot.note)}` : ""}`;
}

function renderTradingMode() {
  const mode = state.book.trading_mode || "paper";
  document.querySelectorAll("#trading-mode [data-mode]").forEach((button) => {
    button.classList.toggle("on", button.dataset.mode === mode);
  });
  const note = document.querySelector("#trading-mode-note");
  if (note && !note.dataset.held) {
    note.textContent = mode === "live"
      ? "Live: the floor trader's next entry goes to CrossTrade. The quantitative trader still books the desk chart on paper. A trade already open on paper finishes on paper."
      : "Paper: both traders book fills from the desk chart. Neither sends an order while paper is on.";
  }
}

async function postExecution(path, body) {
  const note = document.querySelector("#active-note");
  if (note) note.textContent = "Asking the desk…";
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (note) note.textContent = "The desk did not answer.";
    return null;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (note) note.textContent = payload.detail || "The desk refused.";
    return null;
  }
  if (note) note.textContent = payload.note || "";
  const list = document.querySelector("#active-list");
  if (list) delete list.dataset.stamp;
  state.paint = "";
  refresh();
  return payload;
}

let activeEpoch = 0;

function showActive(rows) {
  activeEpoch += 1;
  state.book.active_strategies = rows;
  const list = document.querySelector("#active-list");
  if (list) delete list.dataset.stamp;
  renderActive();
}

async function activeRequest(path, body, failed) {
  const epoch = activeEpoch;
  const note = document.querySelector("#active-note");
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (note) note.textContent = failed;
    return null;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (note) note.textContent = payload.detail || failed;
    return null;
  }
  if (epoch === activeEpoch && Array.isArray(payload.strategies)) {
    const painted = (state.book.active_strategies || []).map((row) => `${row.id}:${row.title}`).join(",");
    state.book.active_strategies = payload.strategies;
    const saved = payload.strategies.map((row) => `${row.id}:${row.title}`).join(",");
    if (saved !== painted) {
      const list = document.querySelector("#active-list");
      if (list) delete list.dataset.stamp;
      renderActive();
    }
  }
  state.paint = "";
  refresh();
  return payload;
}

async function saveCard(row) {
  const payload = await activeRequest(`/api/active-strategies/${encodeURIComponent(row.id)}`, row, "The strategy was not saved.");
  const note = document.querySelector("#active-note");
  if (payload && note) note.textContent = `Saved ${row.title || "the card"} on the desk.`;
  return payload;
}

const activeSaveTimers = new Map();

function queueCardSave(card, wait = 500) {
  const id = card.dataset.id;
  clearTimeout(activeSaveTimers.get(id));
  activeSaveTimers.set(id, setTimeout(() => {
    activeSaveTimers.delete(id);
    const painted = document.querySelector(`#active-list .active-card[data-id="${CSS.escape(id)}"]`);
    if (painted) saveCard(cardFromDom(painted));
  }, wait));
}

async function flushCardSave(card) {
  clearTimeout(activeSaveTimers.get(card.dataset.id));
  activeSaveTimers.delete(card.dataset.id);
  return saveCard(cardFromDom(card));
}

function cardFromDom(card) {
  const typed = card.querySelector("textarea").value;
  const draft = activeDrafts.get(card.dataset.id) || "";
  const text = /strategy\s*\(/.test(typed) || !/strategy\s*\(/.test(draft) ? typed : draft;
  return {
    id: card.dataset.id,
    account: card.querySelector("[data-account]").value,
    text,
    title: (text.match(/strategy\s*\(\s*(?:title\s*=\s*)?"([^"]+)"/) || [])[1] || "Strategy",
    instrument: card.querySelector("[data-contract]").value,
    timeframe: card.querySelector("[data-frame]").value,
    order_instrument: card.querySelector("[data-order]").value.trim(),
    locked: Boolean((state.book.active_strategies || []).find((row) => row.id === card.dataset.id)?.locked),
  };
}

async function loadResearchGuide() {
  const guide = document.querySelector("#research-guide");
  if (!guide || guide.dataset.dirty === "1") return;
  const now = Date.now();
  if (guide.dataset.at && now - Number(guide.dataset.at) < 20000) return;
  guide.dataset.at = String(now);
  try {
    const response = await fetch("/api/research-guide");
    const payload = await response.json();
    if (guide.dataset.dirty === "1") return;
    guide.value = payload.text || "";
  } catch (error) {
    guide.dataset.at = "";
    const note = document.querySelector("#research-guide-note");
    if (note) note.textContent = "The research guide could not be loaded.";
  }
}

async function saveResearchGuide() {
  const guide = document.querySelector("#research-guide");
  const note = document.querySelector("#research-guide-note");
  const button = document.querySelector("#research-guide-save");
  if (!guide || !guide.dataset.at) return;
  if (button) button.disabled = true;
  if (note) note.textContent = "Saving.";
  try {
    const response = await fetch("/api/research-guide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: guide.value }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (note) note.textContent = payload.detail || "Not saved.";
      return;
    }
    guide.value = payload.text || guide.value;
    guide.dataset.dirty = "";
    guide.dataset.at = String(Date.now());
    if (note) note.textContent = "Saved. The researchers read this on the next decision.";
  } catch (error) {
    if (note) note.textContent = "Not saved.";
  } finally {
    if (button) button.disabled = false;
  }
}

document.querySelector("#research-guide")?.addEventListener("input", (event) => {
  event.target.dataset.dirty = "1";
  const note = document.querySelector("#research-guide-note");
  if (note) note.textContent = "";
});
document.querySelector("#research-guide-save")?.addEventListener("click", saveResearchGuide);

let checksFetched = 0;

async function loadChecks() {
  const body = document.querySelector("#tv-checks tbody");
  if (!body || state.channel !== "engine" || Date.now() - checksFetched < 15000) return;
  checksFetched = Date.now();
  let payload = {};
  try {
    payload = await (await fetch("/api/tradingview-checks")).json();
  } catch (error) {
    return;
  }
  const note = document.querySelector("#tv-checks-note");
  if (note && payload.running && !(payload.reports || []).length) note.textContent = "The engine is running each TradingView trade list again.";
  const figures = (m) => m ? `${money(m.net_profit)} · drawdown ${money(m.max_drawdown)} · ${m.trades} trades · ${m.win_rate ?? "–"}% won · factor ${m.profit_factor ?? "–"}` : "";
  body.innerHTML = (payload.reports || []).map((r) => {
    if (r.error) return `<tr><td>${escapeHtml(r.title || r.file || "")}</td><td colspan="5">${escapeHtml(r.error)}</td></tr>`;
    const same = r.identical_from;
    const identical = `${r.matched} of ${r.expected}` + (same ? `. Every trade from ${escapeHtml(same.from)} on (${same.trades}) is identical.` : "");
    const missing = (r.longest_missing || r.missing_bars || []).join("; ") || "None";
    const borrowed = (r.filled_from_yahoo || []).join("; ");
    const where = borrowed ? `${missing}. Filled from Yahoo: ${borrowed}` : missing;
    return `<tr>
      <td>${escapeHtml(r.title)}</td>
      <td>${escapeHtml(r.timeframe)} ${escapeHtml(r.instrument)} from ${escapeHtml((r.range || [""])[0])}</td>
      <td>${identical}</td>
      <td>${figures(r.tradingview)}</td>
      <td>${figures(r.engine)}</td>
      <td>${escapeHtml(where)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="6">${payload.running ? "Running the checks." : "No Strategy Tester trade list is saved in the project root."}</td></tr>`;
}

function kronosBoard(kronos) {
  const row = kronos || {};
  const line = (candle) => `${candle.t || ""} o${candle.o} h${candle.h} l${candle.l} c${candle.c} v${candle.v}`;
  const closed = (row.candles || []).map(line).join("; ");
  const predicted = (row.predicted || []).map(line).join("; ");
  const parts = [
    [row.owner, row.contract, row.timeframe].filter(Boolean).join(" "),
    row.note || "",
    closed ? `Closed candles ${closed}` : "",
    predicted ? `Predicted ${predicted}` : "",
    row.direction || "",
    row.count ? `Stored ${row.count} times for this window.` : "",
    row.reached ? `Five-year walk reached ${row.reached}.` : "",
  ].filter(Boolean);
  return `<h3>Kronos</h3>${parts.map((item) => `<p>${escapeHtml(item)}</p>`).join("")}`;
}

function renderGaps(report) {
  const el = document.querySelector("#rnd-gaps");
  if (!el) return;
  const row = report || {};
  const asked = Boolean(row.at);
  if (!asked) {
    el.innerHTML = "<h3>Portfolio Manager</h3><p>Ask for the backtest report. The table lists the three acceptance misses and the measured result beside the bar.</p>";
    return;
  }
  if (!row.ready || !(row.rows || []).length) {
    el.innerHTML = "<h3>Portfolio Manager</h3><p>The Portfolio Manager is reading the finished backtests.</p>";
    return;
  }
  const passed = Number(row.passed) || 0;
  const counted = Number(row.trials) || 0;
  const head = `${passed.toLocaleString("en-US")} of ${counted.toLocaleString("en-US")} backtests clear every acceptance box that is on.`;
  const body = (row.rows || []).map((item) => `<tr>
    <td>${escapeHtml(item.reason)}</td>
    <td>${escapeHtml(item.typical)}</td>
    <td>${escapeHtml(item.acceptance)}</td>
    <td>${escapeHtml(item.compared)}</td>
    <td>${escapeHtml(item.misses)}</td>
  </tr>`).join("");
  el.innerHTML = `<h3>Portfolio Manager</h3><p>${escapeHtml(head)} Asked ${escapeHtml(row.at)}.</p>
    <table>
      <thead><tr><th>Miss</th><th>Typical result</th><th>Acceptance</th><th>Compared with the bar</th><th>Backtests</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

async function askBacktestReport() {
  const button = document.querySelector("#rnd-report");
  const el = document.querySelector("#rnd-gaps");
  if (button) button.disabled = true;
  if (el) el.innerHTML = "<h3>Portfolio Manager</h3><p>The Portfolio Manager is reading the finished backtests.</p>";
  try {
    const response = await fetch("/api/rnd/acceptance-report", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (el) el.innerHTML = `<h3>Portfolio Manager</h3><p>${escapeHtml(payload.detail || "The Portfolio Manager could not answer.")}</p>`;
      return;
    }
    if (!state.lab) state.lab = {};
    state.lab.acceptance_report = payload;
    renderGaps(payload);
  } catch (error) {
    if (el) el.innerHTML = "<h3>Portfolio Manager</h3><p>The Portfolio Manager could not answer.</p>";
  } finally {
    if (button) button.disabled = false;
  }
}

function renderLab() {
  loadResearchGuide();
  loadChecks();
  renderUnderTheHood();
  const lab = state.lab || {};
  const status = document.querySelector("#engine-status");
  if (status) {
    const count = (lab.trials || []).length;
    status.textContent = lab.running
      ? "The chart developers are running the engines."
      : count
        ? `The chart developers ran ${count} test${count === 1 ? "" : "s"}.`
        : "The chart developers have not run a test yet.";
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
  renderGaps(lab.acceptance_report);
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
    boardEl.innerHTML = now + next + kronosBoard(lab.kronos);
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

let agentTipButton = null;
channelsEl.addEventListener("mouseover", (event) => {
  const button = event.target.closest("button[data-duties]");
  if (!button || button === agentTipButton) return;
  agentTipButton = button;
  showAgentTip(button);
});
channelsEl.addEventListener("mouseout", (event) => {
  const button = event.target.closest("button[data-duties]");
  if (!button || button.contains(event.relatedTarget)) return;
  if (agentTipButton === button) agentTipButton = null;
  hideAgentTip();
});
channelsEl.addEventListener("focusin", (event) => {
  const button = event.target.closest("button[data-duties]");
  if (!button) return;
  agentTipButton = button;
  showAgentTip(button);
});
channelsEl.addEventListener("focusout", (event) => {
  const button = event.target.closest("button[data-duties]");
  if (!button) return;
  agentTipButton = null;
  hideAgentTip();
});
document.querySelector(".rail")?.addEventListener("scroll", () => {
  agentTipButton = null;
  hideAgentTip();
}, { passive: true });

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
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.started === false) throw new Error("start failed");
  } catch (error) {
    startBtn.disabled = false;
    nowText.textContent = "Start did not reach the desk. Try the button again.";
    return;
  }
  state.paint = "";
  refresh();
});

holdBtn.addEventListener("click", async () => {
  if (holdBtn.disabled) return;
  holdBtn.disabled = true;
  const holding = Boolean(state.snapshot && state.snapshot.hold_new);
  const path = holding ? "/api/runs/release-new" : "/api/runs/hold-new";
  try {
    const response = await fetch(path, { method: "POST" });
    if (!response.ok) throw new Error("hold failed");
  } catch (error) {
    holdBtn.disabled = false;
    nowText.textContent = "That control did not reach the desk. Try the button again.";
    return;
  }
  state.paint = "";
  refresh();
});

document.querySelector("#untested-table").addEventListener("click", (event) => {
  const sort = event.target.closest("th[data-sort]");
  if (sort) {
    const key = sort.dataset.sort;
    if (state.untestedSort.key === key) state.untestedSort.dir = state.untestedSort.dir === "asc" ? "desc" : "asc";
    else state.untestedSort = { key, dir: key === "title" ? "asc" : "desc" };
    renderUntested();
    return;
  }
  const remove = event.target.closest("[data-delete]");
  if (remove) {
    deleteLibrary(remove.dataset.delete, remove.dataset.contract);
    return;
  }
  const row = event.target.closest("tr[data-library]");
  if (row) openStrategy(row.dataset.library, "library", row.dataset.contract);
});
document.querySelector("#untested-add").addEventListener("click", openLibraryAdd);

document.querySelector("#library-table").addEventListener("click", (event) => {
  const sort = event.target.closest("th[data-sort]");
  if (sort) {
    const key = sort.dataset.sort;
    if (state.librarySort.key === key) state.librarySort.dir = state.librarySort.dir === "asc" ? "desc" : "asc";
    else state.librarySort = { key, dir: key === "title" ? "asc" : "desc" };
    renderLibrary();
    return;
  }
  const remove = event.target.closest("[data-delete]");
  if (remove) {
    deleteLibrary(remove.dataset.delete, remove.dataset.contract);
    return;
  }
  const row = event.target.closest("tr[data-library]");
  if (row) openStrategy(row.dataset.library, "library", row.dataset.contract);
});
document.querySelector("#library-contract").addEventListener("change", renderLibrary);
document.querySelector("#library-search").addEventListener("input", renderLibrary);
document.querySelector("#indicators-search").addEventListener("input", renderIndicators);
document.querySelector("#indicators-kind").addEventListener("change", renderIndicators);
document.querySelector("#indicators-table").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-indicator]");
  if (row) openStrategy(row.dataset.indicator, "indicators");
});

document.querySelector("#chart-timeframe").addEventListener("change", (event) => {
  const chart = state.charts.find((item) => item.id === state.chartId) || state.charts[0];
  if (!chart) return;
  chart.timeframe = event.target.value;
  chart.follow = true;
  chart.start = 0;
  state.chartData = null;
  ensureChart();
});
document.querySelector("#chart-zoom-in").addEventListener("click", () => zoomChart(-1));
document.querySelector("#chart-zoom-out").addEventListener("click", () => zoomChart(1));
document.querySelector("#chart-tabs").addEventListener("click", (event) => {
  const close = event.target.closest("[data-close]");
  if (close) {
    if (state.charts.length < 2) return;
    const index = state.charts.findIndex((item) => item.id === close.dataset.close);
    if (index < 0) return;
    const removed = state.charts.splice(index, 1)[0];
    if (state.chartId === removed.id) {
      state.chartId = state.charts[Math.max(0, index - 1)].id;
      state.chartData = null;
    }
    ensureChart();
    return;
  }
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
document.querySelector("#chart-scroll").addEventListener("input", (event) => {
  const chart = state.charts.find((item) => item.id === state.chartId);
  if (!chart || !state.chartData) return;
  chart.start = Number(event.target.value);
  chart.follow = chart.start >= Number(event.target.max);
  drawCandles(state.chartData);
});
const chartCanvas = document.querySelector("#chart-canvas");
chartCanvas.addEventListener("wheel", (event) => {
  if (state.channel !== "chart" || !state.chartData) return;
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault();
    zoomChart(event.deltaY > 0 ? 1 : -1);
    return;
  }
  event.preventDefault();
  const chart = state.charts.find((item) => item.id === state.chartId);
  const bar = document.querySelector("#chart-scroll");
  if (!chart || !bar || bar.disabled) return;
  const step = event.deltaY > 0 ? 12 : -12;
  chart.start = Math.max(0, Math.min(Number(bar.max), (chart.start || 0) + step));
  chart.follow = chart.start >= Number(bar.max);
  drawCandles(state.chartData);
}, { passive: false });
chartCanvas.addEventListener("pointerdown", (event) => {
  const chart = state.charts.find((item) => item.id === state.chartId);
  if (!chart) return;
  chartCanvas.setPointerCapture(event.pointerId);
  state.chartDrag = { x: event.clientX, start: chart.start || 0 };
});
chartCanvas.addEventListener("pointermove", (event) => {
  if (!state.chartDrag || !state.chartData) return;
  const chart = state.charts.find((item) => item.id === state.chartId);
  const bar = document.querySelector("#chart-scroll");
  if (!chart || !bar) return;
  const bars = Math.round((state.chartDrag.x - event.clientX) / (state.chartSlot || 8));
  chart.start = Math.max(0, Math.min(Number(bar.max), state.chartDrag.start + bars));
  chart.follow = chart.start >= Number(bar.max);
  drawCandles(state.chartData);
});
chartCanvas.addEventListener("pointerup", () => {
  state.chartDrag = null;
});
const chartStage = document.querySelector(".chart-stage");
if (chartStage && window.ResizeObserver) {
  let chartFit = "";
  new ResizeObserver(() => {
    if (state.channel !== "chart") return;
    const next = `${chartStage.clientWidth}x${chartStage.clientHeight}`;
    if (next === chartFit) return;
    chartFit = next;
    fitChartCanvas();
    if (state.chartData) drawCandles(state.chartData);
  }).observe(chartStage);
}
document.querySelector("#chart-indicators").addEventListener("change", (event) => {
  const box = event.target.closest("input[data-indicator]");
  if (!box) return;
  const chart = state.charts.find((item) => item.id === state.chartId);
  if (!chart) return;
  if (!chart.indicators) chart.indicators = { volume: true, macd: true, delta: true };
  chart.indicators[box.dataset.indicator] = box.checked;
  if (state.chartData) drawCandles(state.chartData);
});

document.querySelector("#lab-run").addEventListener("click", runLab);
document.querySelector("#rnd-run").addEventListener("click", runLab);
document.querySelector("#rnd-report").addEventListener("click", askBacktestReport);

function pressEngineButton(button, path, label) {
  const card = button.closest("[data-engine]");
  const id = card && card.dataset.engine;
  if (!id || button.dataset.sent === "1") return;
  button.dataset.sent = "1";
  button.disabled = false;
  button.textContent = label;
  fetch(`/api/engines/${id}/${path}`, { method: "POST", keepalive: true }).catch(() => {}).finally(() => {
    window.setTimeout(() => {
      button.dataset.sent = "";
      button.disabled = false;
      button.textContent = path === "skip" ? "Skip" : "Restart";
    }, 280);
  });
}

const hoodGrid = document.querySelector("#hood-grid");
hoodGrid.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  const skip = event.target.closest(".hood-skip:not(.hood-restart)");
  const restart = event.target.closest(".hood-restart");
  const button = skip || restart;
  if (!button) return;
  event.preventDefault();
  pressEngineButton(button, skip ? "skip" : "restart", skip ? "Skipping" : "Restarting");
});
hoodGrid.addEventListener("click", (event) => {
  const skip = event.target.closest(".hood-skip:not(.hood-restart)");
  const restart = event.target.closest(".hood-restart");
  const button = skip || restart;
  if (!button) return;
  pressEngineButton(button, skip ? "skip" : "restart", skip ? "Skipping" : "Restarting");
});

const acceptanceForm = document.querySelector("#acceptance-rules");
acceptanceForm.addEventListener("focusin", () => {
  acceptanceForm.dataset.editing = "1";
});
function acceptanceSaved(rules) {
  const on = [];
  if (rules.use_drawdown !== false) on.push(`drawdown under ${rules.drawdown_pct}% of the account`);
  if (rules.use_trades !== false) on.push(`at least ${rules.min_trades} trades`);
  if (rules.use_profit) on.push(`profit above $${Number(rules.min_profit).toLocaleString("en-US")}`);
  if (rules.use_factor) on.push(`profit factor ${rules.profit_factor}`);
  if (rules.use_win_rate) on.push(`win rate ${rules.win_rate}%`);
  const goal = goalLabel(rules);
  return `Saved. A strategy is filed when ${on.join(", ") || "no number box is turned on"}. The scale goal is ${goal}.`;
}

acceptanceForm.addEventListener("invalid", (event) => {
  event.preventDefault();
  const note = document.querySelector("#acceptance-note");
  if (!note) return;
  note.classList.remove("saved");
  note.textContent = "Those numbers were not saved. Use a profit of 0 or more, a profit factor above 0, a win rate from 0 to 100, and at least 1 trade.";
}, true);
acceptanceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const note = document.querySelector("#acceptance-note");
  if (!form.reportValidity()) return;
  const body = {
    min_profit: Number(form.min_profit.value),
    profit_factor: Number(form.profit_factor.value),
    win_rate: Number(form.win_rate.value),
    min_trades: Number(form.min_trades.value),
    drawdown_pct: Number(form.drawdown_pct.value),
    goal: form.goal.value,
    use_drawdown: form.use_drawdown.checked,
    use_trades: form.use_trades.checked,
    use_profit: form.use_profit.checked,
    use_factor: form.use_factor.checked,
    use_win_rate: form.use_win_rate.checked,
  };
  let response;
  let payload = {};
  try {
    response = await fetch("/api/strategy-acceptance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    payload = await response.json().catch(() => ({}));
  } catch (error) {
    if (note) {
      note.classList.remove("saved");
      note.textContent = "The acceptance numbers were not saved.";
    }
    return;
  }
  const saved = payload.strategy_acceptance;
  if (response.ok && saved) {
    state.book.strategy_acceptance = saved;
    form.dataset.stamp = acceptanceStamp(saved);
    form.min_profit.value = saved.min_profit;
    form.profit_factor.value = saved.profit_factor;
    form.win_rate.value = saved.win_rate;
    form.min_trades.value = saved.min_trades;
    form.drawdown_pct.value = saved.drawdown_pct ?? 5;
    form.goal.value = saved.goal || "min_trades";
    form.use_drawdown.checked = saved.use_drawdown !== false;
    form.use_trades.checked = saved.use_trades !== false;
    form.use_profit.checked = saved.use_profit === true;
    form.use_factor.checked = saved.use_factor === true;
    form.use_win_rate.checked = saved.use_win_rate === true;
    if (note) {
      note.classList.add("saved");
      note.textContent = acceptanceSaved(saved);
    }
  } else if (note) {
    note.classList.remove("saved");
    note.textContent = payload.detail || "The acceptance numbers were not saved.";
  }
  delete form.dataset.editing;
  state.paint = "";
  refresh();
});

const rulesForm = document.querySelector("#account-rules");
if (rulesForm) {
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
}

const leaderList = document.querySelector("#leader-list");
if (leaderList) leaderList.addEventListener("click", async (event) => {
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
  delete leaderList.dataset.stamp;
  state.paint = "";
  refresh();
});

document.querySelector("#active-add").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    for (const card of document.querySelectorAll("#active-list .active-card")) {
      if (activeSaveTimers.has(card.dataset.id)) await flushCardSave(card);
    }
    const payload = await activeRequest("/api/active-strategies/add", { id: `strategy-${Date.now()}`, account: "paper", instrument: "ES1!", timeframe: "5m" }, "The card was not added.");
    if (payload) {
      showActive(payload.strategies);
      const note = document.querySelector("#active-note");
      if (note) note.textContent = "Added a card. Paste a Pine strategy into it.";
    }
  } finally {
    button.disabled = false;
  }
});

document.querySelector("#active-list").addEventListener("input", (event) => {
  const card = event.target.closest(".active-card");
  if (!card) return;
  if (event.target.matches("textarea")) {
    activeDrafts.set(card.dataset.id, event.target.value);
    armStart(card);
    paintCardHeading(card);
  }
  queueCardSave(card);
});
document.querySelector("#active-list").addEventListener("change", (event) => {
  const card = event.target.closest(".active-card");
  if (card && event.target.matches("[data-account], [data-contract], [data-frame]")) {
    paintCardHeading(card);
    queueCardSave(card, 0);
  }
});
document.querySelector("#active-list").addEventListener("focusout", () => {
  setTimeout(() => {
    const list = document.querySelector("#active-list");
    if (list?.dataset.held && !list.contains(document.activeElement)) {
      delete list.dataset.held;
      delete list.dataset.stamp;
      renderActive();
    }
  }, 0);
});

document.querySelectorAll("[data-kronos-follow]").forEach((button) => {
  button.addEventListener("click", () => onKronosFollow(button));
});

document.querySelector("#active-list").addEventListener("click", async (event) => {
  const card = event.target.closest(".active-card");
  if (!card) return;
  if (event.target.matches("[data-remove]")) {
    clearTimeout(activeSaveTimers.get(card.dataset.id));
    activeSaveTimers.delete(card.dataset.id);
    event.target.disabled = true;
    const payload = await activeRequest(`/api/active-strategies/${encodeURIComponent(card.dataset.id)}/remove`, null, "The card was not removed.");
    if (payload) {
      activeDrafts.delete(card.dataset.id);
      showActive(payload.strategies);
      const note = document.querySelector("#active-note");
      if (note) note.textContent = "Removed the card.";
    } else {
      event.target.disabled = false;
    }
  }
  if (event.target.matches("[data-submit]")) {
    clearTimeout(activeSaveTimers.get(card.dataset.id));
    activeSaveTimers.delete(card.dataset.id);
    await saveCard({ ...cardFromDom(card), locked: true });
  }
  if (event.target.matches("[data-start]")) {
    event.target.disabled = true;
    const saved = await flushCardSave(card);
    if (!saved) {
      armStart(card);
      return;
    }
    await postExecution(`/api/execution/${encodeURIComponent(card.dataset.id)}/start`);
  }
  if (event.target.matches("[data-stop]")) {
    const slot = (state.book.executing || []).find((row) => row.id === card.dataset.id);
    const live = slot?.position?.mode === "live";
    const open = slot?.position ? (live ? " The open trade is closed with a live flatten order first." : " The open paper trade is closed first.") : "";
    if (!window.confirm(`Stop executing ${slot?.title || "this strategy"}?${open}`)) return;
    event.target.disabled = true;
    await postExecution(`/api/execution/${encodeURIComponent(card.dataset.id)}/stop`);
  }
});

document.querySelector("#trading-mode").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-mode]");
  if (!button) return;
  const mode = button.dataset.mode;
  if (mode === (state.book.trading_mode || "paper")) return;
  if (mode === "live" && !window.confirm("Turn on live trading? The next entry from an executing strategy is sent to CrossTrade on the card's account.")) return;
  const payload = await postExecution("/api/trading-mode", { mode });
  const note = document.querySelector("#trading-mode-note");
  if (payload && note) {
    note.textContent = payload.note;
    note.dataset.held = "1";
    setTimeout(() => delete note.dataset.held, 15000);
  }
});

document.querySelector("#review").addEventListener("click", async () => {
  const response = await fetch("/api/review", { method: "POST" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    document.querySelector("#meeting-note").textContent = body.detail || "The review did not start.";
  }
});

const chatBox = document.querySelector("#chat textarea[name=message]");
function sizeChatBox() {
  chatBox.style.height = "auto";
  chatBox.style.height = `${Math.min(chatBox.scrollHeight, 160)}px`;
}
chatBox.addEventListener("input", sizeChatBox);
chatBox.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.querySelector("#chat").requestSubmit();
  }
});

document.querySelector("#chat").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = event.target.message;
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  sizeChatBox();
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, agent: document.querySelector("#chat-agent")?.value || "Portfolio Manager" }),
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

let lastGaugeScore = null;

function gaugeNumber(score) {
  return score.toFixed(2).replace(/0$/, "").replace(/\.0$/, "");
}

function renderGauge(row) {
  const gauge = document.querySelector("#sentiment-gauge");
  const needle = document.querySelector("#gauge-needle");
  const read = document.querySelector("#gauge-read");
  const value = document.querySelector("#gauge-value");
  const delta = document.querySelector("#gauge-delta");
  const detail = document.querySelector("#gauge-detail");
  if (!gauge || !needle || !read || !value || !delta || !detail || !row) return;
  const score = Number(row.score);
  const angle = Number.isFinite(score) ? (score / 10) * 180 - 90 : 0;
  needle.style.setProperty("--angle", `${angle}deg`);
  value.textContent = Number.isFinite(score) ? gaugeNumber(score) : "\u2014";
  if (lastGaugeScore !== null && Number.isFinite(score)) {
    const change = score - lastGaugeScore;
    delta.hidden = Math.abs(change) < 0.05;
    delta.textContent = gaugeNumber(Math.abs(change));
    delta.className = change > 0 ? "gauge-delta up" : "gauge-delta down";
  }
  if (Number.isFinite(score)) lastGaugeScore = score;
  gauge.dataset.lean = score < 4.5 ? "bear" : score > 5.5 ? "bull" : "flat";
  const session = row.session || {};
  const label = row.band ? `${row.band} ${score}` : "No reading";
  read.setAttribute("aria-label", label);
  read.title = session.start ? `${label}. Last session ${session.start} to ${session.end} ET` : label;
  detail.innerHTML = [row.bull, row.bear, row.risk].filter(Boolean).map((item) =>
    `<p><strong>${escapeHtml(item.name)}.</strong> ${escapeHtml(item.text)}</p>`
  ).join("") + `<p>${(row.sources || []).map((source) => `${escapeHtml(source.kind)}: ${escapeHtml(source.name)}`).join(". ")}</p>`;
}

async function loadGauge() {
  const read = document.querySelector("#gauge-read");
  try {
    const response = await fetch("/api/sentiment");
    if (!response.ok) throw new Error(String(response.status));
    renderGauge(await response.json());
  } catch (error) {
    const value = document.querySelector("#gauge-value");
    if (value) value.textContent = "\u2014";
    if (read) read.setAttribute("aria-label", "Sentiment unavailable");
  }
}

let meetingVoiceFrame = 0;
let meetingAudio = null;
let meetingAnalyser = null;
let meetingAudioContext = null;
let meetingAudioSource = null;

function attachMeetingAudio(audio) {
  meetingAudio = audio || null;
  if (!audio || meetingAudioSource) return;
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return;
  meetingAudioContext = new Context();
  meetingAnalyser = meetingAudioContext.createAnalyser();
  meetingAnalyser.fftSize = 256;
  meetingAudioSource = meetingAudioContext.createMediaElementSource(audio);
  meetingAudioSource.connect(meetingAnalyser);
  meetingAnalyser.connect(meetingAudioContext.destination);
}

function paintMeetingVoice() {
  const room = document.querySelector("#meeting-room");
  const canvas = document.querySelector("#meeting-voice-canvas");
  if (!room || room.hidden || !canvas) return;
  const context = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#000";
  context.fillRect(0, 0, width, height);
  const bars = 48;
  const gap = 4;
  const barWidth = (width - gap * (bars + 1)) / bars;
  const playing = Boolean(meetingAudio && !meetingAudio.paused && !meetingAudio.ended && meetingAnalyser);
  const samples = new Uint8Array(playing ? meetingAnalyser.fftSize : bars);
  if (playing) {
    if (meetingAudioContext && meetingAudioContext.state === "suspended") meetingAudioContext.resume();
    meetingAnalyser.getByteTimeDomainData(samples);
  }
  for (let index = 0; index < bars; index += 1) {
    let level = 0.035;
    if (playing) {
      const sample = samples[Math.floor((index / bars) * samples.length)] || 128;
      level = Math.min(1, Math.abs(sample - 128) / 42);
    }
    const barHeight = Math.max(2, level * height * 0.78);
    const x = gap + index * (barWidth + gap);
    const y = (height - barHeight) / 2;
    const place = index / bars;
    context.fillStyle = place < 0.34 ? "#d5dbe6" : place < 0.66 ? "#e0b15c" : "#f3d7a4";
    context.fillRect(x, y, barWidth, barHeight);
  }
  meetingVoiceFrame = requestAnimationFrame(paintMeetingVoice);
}

function startMeetingVoice() {
  cancelAnimationFrame(meetingVoiceFrame);
  meetingVoiceFrame = requestAnimationFrame(paintMeetingVoice);
}

function stopMeetingVoice() {
  cancelAnimationFrame(meetingVoiceFrame);
  meetingVoiceFrame = 0;
}

function setMeetingMenu(open) {
  const menu = document.querySelector("#call-meeting-menu");
  const button = document.querySelector("#call-meeting-button");
  if (!menu || !button) return;
  menu.hidden = !open;
  button.setAttribute("aria-expanded", open ? "true" : "false");
}

function openPortfolioMeeting() {
  const room = document.querySelector("#meeting-room");
  const speaker = document.querySelector("#meeting-speaker");
  if (!room) return;
  if (speaker) speaker.textContent = "Portfolio Manager";
  room.hidden = false;
  startMeetingVoice();
}

function closePortfolioMeeting() {
  const room = document.querySelector("#meeting-room");
  if (room) room.hidden = true;
  stopMeetingVoice();
}

document.querySelector("#call-meeting-button")?.addEventListener("click", () => {
  const menu = document.querySelector("#call-meeting-menu");
  setMeetingMenu(Boolean(menu && menu.hidden));
});

document.querySelector("#call-meeting-menu")?.addEventListener("click", (event) => {
  const choice = event.target.closest("[data-meeting]");
  if (!choice) return;
  setMeetingMenu(false);
  if (choice.dataset.meeting === "portfolio") openPortfolioMeeting();
});

document.querySelector("#meeting-close")?.addEventListener("click", closePortfolioMeeting);

document.addEventListener("click", (event) => {
  if (!event.target.closest("#call-meeting")) setMeetingMenu(false);
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const room = document.querySelector("#meeting-room");
  if (room && !room.hidden) closePortfolioMeeting();
  setMeetingMenu(false);
});

document.querySelector("#gauge-read")?.addEventListener("click", () => {
  const detail = document.querySelector("#gauge-detail");
  const read = document.querySelector("#gauge-read");
  if (!detail || !read) return;
  const open = detail.hidden;
  detail.hidden = !open;
  read.setAttribute("aria-expanded", open ? "true" : "false");
});

loadGauge();
setInterval(loadGauge, 60000);

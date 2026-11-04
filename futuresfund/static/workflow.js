// Research workflow Design: the research process as a live, editable map.
//
// The diagram comes from /static/workflow_catalog.json. Settings, saved layout, folder counts and the
// queue come from /api/workflow-design, and the change-impact rows from /api/workflow-design/changes.
// Until the desk restarts with that code those routes answer 404: the map still renders from the
// catalog, the settings show the code values read-only, and the proving ledger comes from /api/proving.
// app.js hands this module its /api/state, /api/lab and /api/book while the tab is open; without that
// feed the module polls them itself every 3 s. Everything here is local: no fonts, scripts or images
// load from the internet.

const CATALOG_URL = "/static/workflow_catalog.json?v=8";
const API = "/api/workflow-design";
const CARD_W = 280;
const CARD_H = 170;
const SNAP = 10;
const ZOOM_MIN = 0.16;
const ZOOM_MAX = 2.2;
const STORE_KEY = "wf-design-v1";
const FEED_FRESH_MS = 4500;

const KINDS = {
  folder: { label: "Folder", color: "#38bdf8", note: "A folder of scripts" },
  process: { label: "Step", color: "#60a5fa", note: "A step every study runs" },
  gate: { label: "Gate", color: "#fbbf24", note: "A check that lets a script on or turns it away" },
  stage: { label: "Stage", color: "#34d399", note: "A proving stage; the ring is its pass rate" },
  engine: { label: "Engines", color: "#22d3ee", note: "Backtest engines; the gear spins while one runs" },
  agent: { label: "Agent step", color: "#c084fc", note: "A step an agent answers" },
  library: { label: "Library", color: "#2dd4bf", note: "Where accepted runs are filed" },
  hazard: { label: "Parked", color: "#f87171", note: "Scripts the desk will not run again" },
  mission: { label: "Mission", color: "#67e8f9", note: "The acceptance goal every stage reads" },
  star: { label: "Reference", color: "#facc15", note: "The owner's golden strategy" },
  locked: { label: "Locked", color: "#94a3b8", note: "No research path reaches it today" },
};

const EDGE_KINDS = {
  main: { label: "Main path", color: "#7dd3fc" },
  fail: { label: "Fails or parks", color: "#f87171" },
  loop: { label: "Goes back", color: "#fbbf24" },
  rule: { label: "Acceptance rule", color: "#22d3ee" },
  creation: { label: "New strategies", color: "#a78bfa" },
  golden: { label: "Golden idea", color: "#facc15" },
  locked: { label: "Not reachable", color: "#64748b" },
};

const STATUS_TEXT = { working: "Working now", done: "Done", pending: "Waiting", idle: "Idle", error: "Error", none: "No status light" };
const DAYS_NODE = { 7: "stage7", 30: "stage30", 90: "stage90", 365: "year" };
const NODE_DAYS = { stage7: 7, stage30: 30, stage90: 90, year: 365 };
// Before the first live read: engines 1-3 ES, 4 creation, then three per instrument (roster.ENGINE_DESKS).
const ENGINE_ROOTS = ["ES", "GC", "NQ", "MNQ", "MES", "M2K", "YM"];
const ENGINE_COUNT = ENGINE_ROOTS.length * 3 + 1;
// Engine chips on one card before the rest fold into "+N".
const CARD_CHIPS = 6;
// The setting that turns each step on or off. When it is off the card is shown as skipped.
const MAIN_SWITCH = {
  read: "read_before_testing",
  stage7: "stage_7_on",
  stage30: "stage_30_on",
  stage90: "stage_90_on",
  review: "stage_review",
  recheck: "recheck_before_filing",
};

// The keys objective.acceptance_misses returns, in plain words.
const MISS_TEXT = {
  profit: "Profit",
  drawdown: "Drawdown",
  trades: "Trades",
  trade_loss: "Stop over $750",
  pace: "Over 600 trades a year",
  factor: "Profit factor",
  win_rate: "Win rate",
  under_drawdown: "Profit under drawdown",
  growth: "Recent profit",
  error: "Error",
};
const VERDICTS = {
  toward: { label: "Toward the goal", short: "Toward", color: "#34d399" },
  away: { label: "Away from the goal", short: "Away", color: "#f87171" },
  flat: { label: "No clear change", short: "Flat", color: "#94a3b8" },
  no_trades: { label: "No trades", short: "No trades", color: "#fbbf24" },
};

// The research settings as the code writes them. Used, read-only, until /api/workflow-design answers.
const FALLBACK_KNOBS = [
  ["use_alert_bars", "Add the TradingView alert bars", "bars", "bool", null, null, null, "", true,
    "On: a study adds the TradingView alert candles that came in after the last Databento download, so the 7-day stage tests the latest week. Off: a study uses only the downloaded and exported bars."],
  ["live_refresh", "Read the alert bars again every", "bars", "int", 60, 3600, 60, "seconds", 900,
    "How often the alert candles after the download are read again. A study loads its bars once, when it starts, so a refresh never changes the bars under a search that is running."],
  ["compile_repair_tries", "Compile repairs before parking", "compile", "int", 0, 10, 1, "repairs", 3,
    "A script that does not compile goes to its developer for a repair, and the chart researcher emails the error while the repair runs. After this many repairs that still do not compile, the researcher asks the owner and the script waits in awaitingAnswerStrategies. 0 asks at once."],
  ["read_before_testing", "Read the script before testing", "read", "bool", null, null, null, "", true,
    "On: before the 7-day stage, the desk measures how the script responds to each input, leaves out the inputs that change nothing, and the researcher reads the script once. Off: the stages start at once from the script's own settings."],
  ["response_sessions", "Sessions for the response test", "read", "int", 5, 64, 1, "sessions", 21,
    "How many recent trading sessions the response test runs on. A researcher's rewrite is checked on the same window before it is kept."],
  ["empty_charts", "Empty charts before asking why", "read", "int", 1, 21, 1, "charts", 3,
    "The researcher waits until the script has taken no trades on this many charts, then asks why its entry never passes."],
  ["allow_repair_rewrites", "Let a repair answer rewrite the script", "read", "bool", null, null, null, "", true,
    "On: a repair answer may rewrite the script, even though research otherwise changes input values only. Off: a repair answer changes input values only, like every other answer."],
  ["stage_7_on", "Run the 7-day stage", "stage7", "bool", null, null, null, "", true,
    "Off: the 7-day stage is skipped. The next stage starts from the same settings, and no ledger mark is written for it."],
  ["stage_30_on", "Run the 30-day stage", "stage30", "bool", null, null, null, "", true,
    "Off: the 30-day stage is skipped. The next stage starts from the settings the stage before passed with."],
  ["stage_90_on", "Run the 90-day stage", "stage90", "bool", null, null, null, "", true,
    "Off: the 90-day stage is skipped. The one-year search starts from the settings the stage before passed with."],
  ["stage_7_attempts", "7-day stage attempts", "stage7", "int", 1, 1000, 1, "results", 100,
    "Different results the 7-day stage gets on one chart. A pass does not end the stage: it keeps searching for more profit with less drawdown. A stage that never passes spends them all."],
  ["stage_30_attempts", "30-day stage attempts", "stage30", "int", 1, 1000, 1, "results", 150,
    "Different results the 30-day stage gets on one chart, starting from the best 7-day pass."],
  ["stage_90_attempts", "90-day stage attempts", "stage90", "int", 1, 1000, 1, "results", 200,
    "Different results the 90-day stage gets on one chart, starting from the best 30-day pass. Its best pass starts the one-year search."],
  ["stage_patience", "Attempts without a better pass", "stage7,stage30,stage90", "int", 1, 1000, 1, "attempts", 25,
    "After a stage passes, it stops when this many attempts in a row found no pass with more profit after drawdown."],
  ["trade_cut_attempts", "Trade cut attempts", "tradecut", "int", 1, 50, 1, "attempts", 12,
    "A run with more than 900 trades, or on a stage a pace over 600 trades a year, starts the trade cut on that chart. After this many cut attempts in a row still over the cap, the researcher asks the owner instead of parking the script."],
  ["stage_trades_per_week", "Extra floor of trades a week", "goal", "float", 0, 10, 0.5, "trades a week", 0,
    "0 is off: the fewest trades come from the trade count on the strategy library page, pro-rated to each test. Above 0, every test and the year also need this many trades for each five-session week, when that is more."],
  ["stage_review", "Review failed stages", "review", "bool", null, null, null, "", true,
    "On: a script that fails a 7, 30 or 90-day stage, or the year, goes to the researcher, who looks for why it fails. Off: no failed-stage review is asked."],
  ["stage_reviews", "Failed-stage reviews per script", "review", "int", 0, 21, 1, "reviews", 6,
    "The most failed-stage reviews the researcher does on one script, one for each stage and chart."],
  ["review_backlog", "Review backlog limit", "review", "int", 1, 50, 1, "questions", 12,
    "A new failed-stage review is left out while this many stage questions wait on the one model."],
  ["reopen_for_answers", "Run finished charts again for an answer", "review", "bool", null, null, null, "", true,
    "On: when the researcher answers with new settings or a rewrite, the script's finished charts run again. Off: the answer waits for the next chart that opens the script."],
  ["reopen_limit", "Reopens per script", "review", "int", 0, 10, 1, "times", 3,
    "The most times a finished script's charts run again for a researcher's answer."],
  ["attempt_limit", "Year search attempts", "year", "int", 10, 1000, 1, "results", 300,
    "The most different results the one-year search tries on one chart, after the stages pass. The search does not stop at 10 successful runs. Engine 4's drafts have their own count."],
  ["loss_look", "Leave a losing script after", "year", "int", 5, 400, 1, "results", 40,
    "A chart engine leaves the script when its best one-year result is still a loss after this many different results."],
  ["impact_percent", "Profit drop that ends a climb", "year", "float", 0.01, 0.5, 0.01, "share of profit", 0.05,
    "When moving one input lowers the profit by this share or more (0.05 is 5 percent), the search stops moving that input that way."],
  ["creation_attempts", "Draft search attempts", "engine4", "int", 10, 1000, 1, "results", 100,
    "The most different results engine 4 tries on one new draft. It stops sooner at the first accepted run."],
  ["flat_runs", "No-trade runs before asking the owner", "zero", "int", 3, 100, 1, "runs", 20,
    "After this many one-year runs in a row with no trades and no profit, the chart researcher asks the owner by email and the script waits in awaitingAnswerStrategies, unless it traded in this search or passed a stage."],
  ["recheck_before_filing", "Recheck the last 7, 30 and 90 days before filing", "recheck", "bool", null, null, null, "", false,
    "On: a one-year run that meets acceptance is run again on the last 7, 30 and 90 days, and is filed only when every window passes. Off, as the code is written today: filing does not check the recent windows again."],
  ["email_questions", "Email the researchers' questions", "awaiting", "bool", null, null, null, "", true,
    "On: a script that does not compile after its repairs, gets no entry rule, stops in PineTS, never trades, or still takes too many trades after the trade cut gets a question from its chart researcher, the Portfolio Manager emails it, and the script waits in awaitingAnswerStrategies. Off: those scripts are parked in cannotRunStrategies the old way."],
  ["inbox_poll_seconds", "Read the mailbox for answers every", "awaiting", "int", 30, 3600, 30, "seconds", 120,
    "How often the Portfolio Manager reads the mailbox for replies to the questions it emailed. Only replies from the report address (or QUESTION_FROM) that pass DKIM, SPF or DMARC are used."],
  ["engines_at_once", "Backtests at once", "claim", "int", 1, 64, 1, "workers", 7,
    "The 21 chart engines (three per instrument) and the creation engine share this many PineTS worker processes, so at most this many backtests run at the same time. The code value is one for every core but one, and at least 2."],
].map(([key, label, node, type, min, max, step, unit, value, help]) => ({
  key, label, node, nodes: node.split(","), type, min, max, step, unit, help, value, default: value, changed: false,
}));

const reduceMotion = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false, addEventListener() {} };

const ui = {
  section: null,
  built: false,
  loading: null,
  failed: "",
  catalog: null,
  nodes: [],
  byId: new Map(),
  edges: [],
  base: {},
  pos: {},
  layoutSource: "catalog",
  layoutDirty: false,
  world: { width: 2800, height: 1600 },
  design: null,
  api: "unknown",
  changes: null,
  changesApi: "unknown",
  proving: null,
  live: { snapshot: null, lab: null, book: null, fedAt: 0 },
  cam: { x: 0, y: 0, k: 0.5 },
  userMoved: false,
  selected: null,
  hover: null,
  edit: false,
  drag: null,
  draft: {},
  knobNote: null,
  queueDraft: null,
  queueNote: null,
  layoutNote: "",
  filter: "all",
  hidden: new Set(),
  impactOpen: false,
  legendOpen: false,
  visible: false,
  polling: 0,
  timers: {},
  spots: {},
  loads: {},
  particleKey: "",
  els: {},
  liveQueued: false,
  flight: 0,
};

// ---------- small helpers ----------

function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch]));
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function remember(key, fallback) {
  try {
    const all = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    return key in all ? all[key] : fallback;
  } catch (error) {
    return fallback;
  }
}

function keep(key, value) {
  try {
    const all = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    if (value === undefined) delete all[key];
    else all[key] = value;
    localStorage.setItem(STORE_KEY, JSON.stringify(all));
  } catch (error) {
    // Browser storage is only a convenience here.
  }
}

function money(value, signed = false) {
  const number = Number(value);
  if (value == null || !Number.isFinite(number)) return "—";
  const sign = number < 0 ? "−" : signed && number > 0 ? "+" : "";
  return `${sign}$${Math.abs(number).toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
}

function plainNumber(value, digits = 2, signed = false) {
  const number = Number(value);
  if (value == null || !Number.isFinite(number)) return "—";
  const sign = number < 0 ? "−" : signed && number > 0 ? "+" : "";
  return `${sign}${Math.abs(number).toLocaleString("en-US", { maximumFractionDigits: digits })}`;
}

function mix(hex, other, share) {
  const parse = (value) => {
    const text = value.replace("#", "");
    return [0, 2, 4].map((at) => parseInt(text.slice(at, at + 2), 16));
  };
  const a = parse(hex);
  const b = parse(other);
  return `#${a.map((part, index) => Math.round(part + (b[index] - part) * share).toString(16).padStart(2, "0")).join("")}`;
}

function kindOf(node) {
  return KINDS[node?.kind] ? node.kind : "process";
}

function colorOf(node) {
  return KINDS[kindOf(node)].color;
}

function agentMeta(name) {
  return ui.catalog?.agents?.[name] || { icon: "developer", color: "#94a3b8", role: "" };
}

function engineIdOf(label) {
  const match = /^E(\d+)/i.exec(String(label || ""));
  return match ? Number(match[1]) : null;
}

function engineRows() {
  const rows = ui.live.lab?.engines;
  return Array.isArray(rows) ? rows : [];
}

function engineById(id) {
  return engineRows().find((engine) => Number(engine.id) === Number(id)) || null;
}

// The creation engine says so on its row. Before the first live read it is engine 4.
function isCreation(engine) {
  if (!engine) return false;
  if (engine.creation != null) return Boolean(engine.creation);
  return Number(engine.id) === 4;
}

function creationEngine() {
  return engineRows().find(isCreation) || null;
}

// "NQ 5m" for a chart engine, "creation" for the creation engine, from the live row.
function engineChart(id) {
  const engine = engineById(id);
  if (engine) {
    if (isCreation(engine)) return "creation";
    const root = engine.root || engine.instrument || "";
    return `${root} ${engine.timeframe || ""}`.trim();
  }
  const number = Number(id);
  if (number === 4) return "creation";
  const index = number < 4 ? number - 1 : number - 2;
  const root = ENGINE_ROOTS[Math.floor(index / 3)];
  return root ? `${root} ${["2m", "5m", "15m"][index % 3]}` : "";
}

function statusOf(name) {
  const status = ui.live.snapshot?.agent_status;
  if (!status || !(name in status)) return "none";
  return status[name] || "idle";
}

function latestLine(name) {
  const logs = ui.live.snapshot?.agent_logs?.[name];
  if (Array.isArray(logs) && logs.length) return logs[logs.length - 1];
  return ui.live.snapshot?.agent_tasks?.[name] || "";
}

function editable() {
  return ui.api === "ok";
}

function knobRows() {
  const rows = ui.design?.knobs;
  return Array.isArray(rows) && rows.length ? rows : FALLBACK_KNOBS;
}

function knobNodes(knob) {
  if (Array.isArray(knob.nodes) && knob.nodes.length) return knob.nodes;
  return String(knob.node || "").split(",").map((part) => part.trim()).filter(Boolean);
}

function knobsFor(node) {
  const listed = new Set(node.knobs || []);
  return knobRows().filter((knob) => listed.has(knob.key) || knobNodes(knob).includes(node.id));
}

function knobValue(key) {
  if (key in ui.draft) return ui.draft[key];
  const knob = knobRows().find((row) => row.key === key);
  return knob ? knob.value : undefined;
}

function savedValue(key) {
  const knob = knobRows().find((row) => row.key === key);
  return knob ? knob.value : undefined;
}

function shownValue(knob, value) {
  if (knob.type === "bool") return value ? "On" : "Off";
  const unit = knob.unit ? ` ${knob.unit}` : "";
  return `${plainNumber(value, 2)}${unit}`;
}

// ---------- icons ----------

function gearPath(cx, cy, inner, outer, teeth) {
  const step = (Math.PI * 2) / teeth;
  const points = [];
  for (let tooth = 0; tooth < teeth; tooth += 1) {
    const angle = tooth * step;
    for (const [offset, radius] of [[-0.29, inner], [-0.16, outer], [0.16, outer], [0.29, inner]]) {
      const at = angle + offset * step;
      points.push(`${(cx + Math.cos(at) * radius).toFixed(2)} ${(cy + Math.sin(at) * radius).toFixed(2)}`);
    }
  }
  return `M${points.join("L")}Z`;
}

const INK = "#050b16";

const GLYPHS = {
  folder: (F, S) => `
    <path d="M10 18h13l4 4h24a4 4 0 0 1 4 4v3H10z" fill="${S}" stroke="currentColor" stroke-opacity=".45"/>
    <rect x="15" y="11" width="28" height="19" rx="2.5" fill="#e2e8f0" fill-opacity=".8" stroke="none"/>
    <rect x="21" y="7" width="28" height="19" rx="2.5" fill="#f8fafc" fill-opacity=".5" stroke="none"/>
    <path d="M25 13h17M25 18h12" stroke="${INK}" stroke-opacity=".35"/>
    <path d="M5 27a4 4 0 0 1 4-4h14l4 4h28a4 4 0 0 1 4 4v19a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4z" fill="${F}" stroke="currentColor"/>
    <path d="M8 33h48" stroke="#fff" stroke-opacity=".35"/>`,
  files: (F, S) => `
    <path d="M17 5h23l12 12v38a3 3 0 0 1-3 3H17a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3z" fill="${S}" stroke="currentColor"/>
    <path d="M40 5v9a3 3 0 0 0 3 3h9" stroke="currentColor"/>
    <path d="M20 24h20M20 30h25M20 36h16" stroke="currentColor" stroke-opacity=".6"/>
    <rect x="8" y="41" width="36" height="14" rx="3.5" fill="${F}" stroke="none"/>
    <text x="26" y="51.5" text-anchor="middle" font-size="9.5" font-weight="900" fill="${INK}" stroke="none" letter-spacing="1.2" font-family="Bahnschrift, Segoe UI, sans-serif">PINE</text>`,
  queue: (F, S) => `
    <rect x="6" y="9" width="40" height="12" rx="4" fill="${F}" stroke="none"/>
    <rect x="6" y="26" width="40" height="12" rx="4" fill="${S}" stroke="currentColor"/>
    <rect x="6" y="43" width="40" height="12" rx="4" fill="${S}" stroke="currentColor" stroke-opacity=".5"/>
    <circle cx="13" cy="15" r="2.6" fill="${INK}" stroke="none"/>
    <path d="M19 15h20" stroke="${INK}" stroke-width="2.6"/>
    <circle cx="13" cy="32" r="2.4" fill="currentColor" stroke="none"/>
    <circle cx="13" cy="49" r="2.4" fill="currentColor" fill-opacity=".6" stroke="none"/>
    <path d="M55 12v38m-6-6 6 7 6-7" stroke="currentColor" stroke-width="2.6"/>`,
  engine: (F) => `
    <g class="wf-gear wf-gear-a"><path d="${gearPath(25, 27, 15, 21, 10)}" fill="${F}" stroke="#fff" stroke-opacity=".35"/>
      <circle cx="25" cy="27" r="8" fill="${INK}" stroke="currentColor"/><circle cx="25" cy="27" r="3" fill="currentColor" stroke="none"/></g>
    <g class="wf-gear wf-gear-b"><path d="${gearPath(47.5, 46.5, 8.5, 12.5, 8)}" fill="${F}" stroke="#fff" stroke-opacity=".35"/>
      <circle cx="47.5" cy="46.5" r="4.2" fill="${INK}" stroke="currentColor"/></g>`,
  gate: (F, S) => `
    <path d="M32 4l23 8.5v16.5c0 14.5-9.8 24.6-23 31-13.2-6.4-23-16.5-23-31V12.5z" fill="${S}" stroke="currentColor"/>
    <path d="M32 11l16.5 6.2v12.4c0 10.4-7 18-16.5 22.6-9.5-4.6-16.5-12.2-16.5-22.6V17.2z" fill="${F}" stroke="none"/>
    <path d="M24 31l6 6 11-13" stroke="${INK}" stroke-width="4.2"/>`,
  library: (F, S) => `
    <rect x="7" y="11" width="10" height="42" rx="1.6" fill="${F}" stroke="none"/>
    <rect x="19" y="17" width="9" height="36" rx="1.6" fill="${S}" stroke="currentColor"/>
    <rect x="30" y="8" width="11" height="45" rx="1.6" fill="${F}" fill-opacity=".85" stroke="none"/>
    <rect x="44" y="13" width="9" height="39" rx="1.6" transform="rotate(13 48.5 32.5)" fill="${S}" stroke="currentColor"/>
    <path d="M7 17h10M7 46h10M30 14h11M30 46h11" stroke="${INK}" stroke-opacity=".45"/>
    <path d="M4 56h56" stroke="currentColor" stroke-width="3"/>`,
  hazard: (F, S) => `
    <rect x="6" y="12" width="52" height="42" rx="4.5" fill="${S}" stroke="currentColor"/>
    <path d="M6 16.5a4.5 4.5 0 0 1 4.5-4.5h43a4.5 4.5 0 0 1 4.5 4.5V22H6z" fill="${F}" stroke="none"/>
    <path d="M13 12l-6 10M23 12l-6 10M33 12l-6 10M43 12l-6 10M53 12l-6 10" stroke="${INK}" stroke-width="3.6" stroke-linecap="butt"/>
    <path d="M32 27l12.5 21h-25z" fill="${F}" stroke="${INK}" stroke-width="1.6"/>
    <path d="M32 33.5v7" stroke="${INK}" stroke-width="3.2"/>
    <circle cx="32" cy="44.6" r="1.8" fill="${INK}" stroke="none"/>`,
  mission: (F, S) => `
    <circle cx="29" cy="35" r="24" fill="${S}" stroke="currentColor"/>
    <circle cx="29" cy="35" r="16" stroke="currentColor" stroke-opacity=".75"/>
    <circle cx="29" cy="35" r="8.5" fill="${F}" stroke="none"/>
    <path d="M29 5v7M29 58v4M1 35h5M52 35h4" stroke="currentColor" stroke-opacity=".5"/>
    <path d="M29 35L53 11" stroke="#f8fafc" stroke-width="2.6"/>
    <path d="M47 7l1 6 6 1M51 3l1 6 6 1" stroke="#f8fafc" stroke-width="2.2"/>`,
  star: (F) => `
    <path d="M32 4.5l8.1 16.4 18.1 2.7-13.1 12.7 3.1 18L32 45.8l-16.2 8.5 3.1-18L5.8 23.6l18.1-2.7z" fill="${F}" stroke="#fff" stroke-opacity=".55"/>
    <path d="M32 13l5.2 10.6 11.6 1.7" stroke="#fff" stroke-opacity=".7"/>
    <path d="M55 46l1.4 3.6L60 51l-3.6 1.4L55 56l-1.4-3.6L50 51l3.6-1.4z" fill="#fff7cc" stroke="none"/>
    <path d="M9 44l1 2.5 2.5 1-2.5 1L9 51l-1-2.5-2.5-1 2.5-1z" fill="#fff7cc" stroke="none"/>`,
  lock: (F) => `
    <path d="M20 29v-8a12 12 0 0 1 24 0v8" stroke="currentColor" stroke-width="5.2"/>
    <rect x="11" y="27" width="42" height="31" rx="6.5" fill="${F}" stroke="#fff" stroke-opacity=".3"/>
    <path d="M11 37h42" stroke="#fff" stroke-opacity=".18"/>
    <circle cx="32" cy="41" r="4.6" fill="${INK}" stroke="none"/>
    <path d="M32 43v8" stroke="${INK}" stroke-width="3.6"/>`,
  bars: (F) => `
    <path d="M4 58h56" stroke="currentColor" stroke-opacity=".45"/>
    <path d="M14 9v42M32 5v38M50 15v40" stroke="currentColor" stroke-opacity=".75"/>
    <rect x="8.5" y="17" width="11" height="25" rx="2" fill="#34d399" stroke="none"/>
    <rect x="26.5" y="11" width="11" height="21" rx="2" fill="#f87171" stroke="none"/>
    <rect x="44.5" y="23" width="11" height="25" rx="2" fill="${F}" stroke="none"/>
    <path d="M8 51l14-9 12 5 16-15" stroke="#f8fafc" stroke-opacity=".8" stroke-width="1.8"/>`,
  compile: (F, S) => `
    <rect x="5" y="8" width="46" height="38" rx="6.5" fill="${S}" stroke="currentColor"/>
    <path d="M5 16h46" stroke="currentColor" stroke-opacity=".4"/>
    <circle cx="10.5" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="15.5" cy="12" r="1.4" fill="currentColor" stroke="none"/>
    <path d="M20 23l-7 7 7 7M36 23l7 7-7 7M31 21l-6 19" stroke="currentColor" stroke-width="3"/>
    <g class="wf-gear wf-gear-c"><path d="${gearPath(49, 49, 7, 11, 8)}" fill="${F}" stroke="#fff" stroke-opacity=".35"/><circle cx="49" cy="49" r="3.4" fill="${INK}" stroke="none"/></g>`,
  rule: (F, S) => `
    <path d="M38 6h14a4 4 0 0 1 4 4v44a4 4 0 0 1-4 4H38" fill="${S}" stroke="currentColor"/>
    <path d="M44 6v52" stroke="currentColor" stroke-opacity=".35"/>
    <rect x="4" y="9" width="22" height="13" rx="4" fill="${F}" stroke="none"/>
    <text x="15" y="19" text-anchor="middle" font-size="9.5" font-weight="900" fill="${INK}" stroke="none" font-family="Bahnschrift, Segoe UI, sans-serif">IF</text>
    <path d="M5 38h32" stroke="currentColor" stroke-width="4.2"/>
    <path d="M26 27l12 11-12 11" stroke="currentColor" stroke-width="4.2"/>`,
  review: (F, S) => `
    <rect x="8" y="9" width="35" height="48" rx="4.5" fill="${S}" stroke="currentColor"/>
    <rect x="17" y="5" width="17" height="9" rx="2.5" fill="${F}" stroke="none"/>
    <path d="M15 24h21M15 31h15M15 38h10" stroke="currentColor" stroke-opacity=".65"/>
    <circle cx="42" cy="40" r="10.5" fill="${INK}" fill-opacity=".85" stroke="${F}" stroke-width="3.4"/>
    <path d="M37.5 40.5l3 3 6-7" stroke="currentColor" stroke-width="2.4"/>
    <path d="M49.5 47.5l8 8" stroke="currentColor" stroke-width="5"/>`,
  archive: (F, S) => `
    <rect x="5" y="9" width="54" height="13" rx="3.5" fill="${F}" stroke="none"/>
    <path d="M9 22h46v29a4 4 0 0 1-4 4H13a4 4 0 0 1-4-4z" fill="${S}" stroke="currentColor"/>
    <rect x="23" y="29" width="18" height="7" rx="3.5" stroke="currentColor"/>
    <path d="M14 44h36" stroke="currentColor" stroke-opacity=".35"/>
    <path d="M5 15.5h54" stroke="#fff" stroke-opacity=".3"/>`,
  trophy: (F, S) => `
    <path d="M19 7h26v14a13 13 0 0 1-26 0z" fill="${F}" stroke="#fff" stroke-opacity=".4"/>
    <path d="M19 11h-8a8.5 8.5 0 0 0 9 13M45 11h8a8.5 8.5 0 0 1-9 13" stroke="currentColor" stroke-width="3"/>
    <path d="M32 11.5l2 4.2 4.6.6-3.4 3.2.9 4.6-4.1-2.3-4.1 2.3.9-4.6-3.4-3.2 4.6-.6z" fill="#fff7cc" stroke="none"/>
    <path d="M28 34h8v8h-8z" fill="${F}" stroke="none"/>
    <rect x="20" y="42" width="24" height="8" rx="2" fill="${S}" stroke="currentColor"/>
    <rect x="15" y="50" width="34" height="7" rx="2.5" fill="${F}" stroke="none"/>`,
  draft: (F, S) => `
    <path d="M11 5h26l11 11v40a3 3 0 0 1-3 3H11a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3z" fill="${S}" stroke="currentColor"/>
    <path d="M37 5v9a2 2 0 0 0 2 2h9" stroke="currentColor"/>
    <path d="M14 24h18M14 31h22M14 38h12" stroke="currentColor" stroke-opacity=".55" stroke-dasharray="3 3.5"/>
    <g transform="rotate(38 46 38)"><rect x="41.5" y="15" width="9" height="34" rx="1.8" fill="${F}" stroke="none"/>
      <path d="M41.5 49l4.5 9 4.5-9z" fill="#fde68a" stroke="none"/><rect x="41.5" y="15" width="9" height="5.5" fill="#f9a8d4" stroke="none"/></g>`,
  mini_agent: (F, S) => `
    <circle cx="32" cy="22" r="11" fill="${F}" stroke="none"/>
    <rect x="22" y="19" width="20" height="6" rx="3" fill="${INK}" stroke="none"/>
    <path d="M10 58c2-12 11-18 22-18s20 6 22 18" fill="${S}" stroke="currentColor"/>`,
};

function glyph(name, kind) {
  const F = `url(#wf-gk-${kind})`;
  const S = `url(#wf-gs-${kind})`;
  const body = (GLYPHS[name] || GLYPHS.files)(F, S);
  return `<svg class="wf-glyph wf-glyph-${esc(name)}" viewBox="0 0 64 64" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
}

const RING = 2 * Math.PI * 28.5;

function stageBadge(node) {
  const label = esc(node.badge || "");
  const small = label.length > 2;
  return `<svg class="wf-glyph wf-level" viewBox="0 0 64 64" aria-hidden="true">
    <circle cx="32" cy="32" r="28.5" fill="none" stroke="rgba(148,163,184,.18)" stroke-width="4"/>
    <circle class="wf-ring" cx="32" cy="32" r="28.5" fill="none" stroke="currentColor" stroke-width="4" stroke-linecap="round"
      stroke-dasharray="${RING.toFixed(2)}" stroke-dashoffset="${RING.toFixed(2)}" transform="rotate(-90 32 32)"/>
    <path d="M32 10.5l18.6 10.75v21.5L32 53.5 13.4 42.75v-21.5z" fill="url(#wf-gk-stage)" stroke="#fff" stroke-opacity=".35" stroke-width="1.4"/>
    <path d="M32 15l14.7 8.5" stroke="#fff" stroke-opacity=".45" stroke-width="1.6" fill="none" stroke-linecap="round"/>
    <text x="32" y="${small ? 36.6 : 38}" text-anchor="middle" font-size="${small ? 13 : 16}" font-weight="900" fill="${INK}"
      font-family="Bahnschrift, Segoe UI, sans-serif" letter-spacing=".5">${label}</text>
  </svg>`;
}

const ACCESSORY = {
  researcher: (c) => `<circle cx="35" cy="35" r="6.2" fill="rgba(255,255,255,.18)" stroke="#f8fafc" stroke-width="2.2"/><path d="M39.6 39.6l5.2 5.2" stroke="#f8fafc" stroke-width="3.2" stroke-linecap="round"/><path d="M32.5 32.6a3 3 0 0 1 3-1.8" stroke="${c}" stroke-width="1.4" fill="none" stroke-linecap="round"/>`,
  developer: (c) => `<path d="M14.5 20a9.5 9.5 0 0 1 19 0" stroke="#0f172a" stroke-width="2.6" fill="none"/><rect x="12" y="17.5" width="4.6" height="8" rx="2.3" fill="${c}"/><rect x="31.4" y="17.5" width="4.6" height="8" rx="2.3" fill="${c}"/><path d="M33.5 25.5c0 3.6-2.6 5.4-6.4 5.4" stroke="#0f172a" stroke-width="1.6" fill="none"/><text x="24" y="44.5" text-anchor="middle" font-size="7.4" font-weight="900" fill="#06101f" font-family="Consolas, monospace">&lt;/&gt;</text>`,
  analyst: () => `<path d="M14.5 44l5-4.6 4.2 2.6 5.6-6.4 4.6 2.4" stroke="#06101f" stroke-width="2.1" fill="none" stroke-linecap="round" stroke-linejoin="round"/><circle cx="33.9" cy="38" r="1.7" fill="#06101f"/><circle cx="28.6" cy="20" r="4.1" fill="none" stroke="#fde68a" stroke-width="1.5"/><path d="M32.4 21.6l2.5 6" stroke="#fde68a" stroke-width="1.2"/>`,
  checker: (c) => `<circle cx="24" cy="40.5" r="5.4" fill="#06101f"/><path d="M21.4 40.6l1.9 1.9 3.6-4" stroke="${c}" stroke-width="1.9" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M15.5 13.5h17" stroke="${c}" stroke-width="2.2" stroke-linecap="round"/>`,
  tester: (c) => `<rect x="15.2" y="10.4" width="17.6" height="4.6" rx="2.3" fill="#0f172a"/><circle cx="19.6" cy="12.7" r="2.1" fill="${c}"/><circle cx="28.4" cy="12.7" r="2.1" fill="${c}"/><path d="M34.5 29.5h7M36 29.5v4.5l-4.6 8.4h12.2L39 34v-4.5" fill="rgba(255,255,255,.22)" stroke="#f8fafc" stroke-width="1.5" stroke-linejoin="round"/><path d="M33.3 39.5h9.4" stroke="${c}" stroke-width="2.4"/>`,
  writer: (c) => `<path d="M37.5 30c3.2-9 9-12.5 9-12.5s-.6 7.6-6.6 13.6l-3.6 1.4z" fill="#f8fafc"/><path d="M36.4 33.2l-3.6 6.4" stroke="#f8fafc" stroke-width="1.6" stroke-linecap="round"/><path d="M40.2 25.4l3.4-3.6" stroke="${c}" stroke-width="1.2"/>`,
  lesson: (c) => `<path d="M24 5.5l13 5.2-13 5.2-13-5.2z" fill="#0f172a" stroke="${c}" stroke-width="1.2" stroke-linejoin="round"/><path d="M16 12.6v3.6c2.4 1.6 13.6 1.6 16 0v-3.6" fill="#0f172a"/><path d="M35.4 11.4v7" stroke="${c}" stroke-width="1.3"/><circle cx="35.4" cy="19.4" r="1.6" fill="${c}"/>`,
  cards: (c) => `<g transform="translate(36 30)"><rect x="-4" y="-7" width="8" height="11" rx="1.4" transform="rotate(-24)" fill="#f8fafc" stroke="#0f172a" stroke-width=".8"/><rect x="-4" y="-7" width="8" height="11" rx="1.4" fill="#f8fafc" stroke="#0f172a" stroke-width=".8"/><rect x="-4" y="-7" width="8" height="11" rx="1.4" transform="rotate(24)" fill="${c}" stroke="#0f172a" stroke-width=".8"/><path d="M5.4 -4.2l1.2 2.6" stroke="#0f172a" stroke-width=".8"/></g>`,
  risk: (c) => `<path d="M14.6 17.5a9.4 9.4 0 0 1 18.8 0" fill="${c}" fill-opacity=".8"/><path d="M24 7.5v4" stroke="${c}" stroke-width="2.4" stroke-linecap="round"/><path d="M24 34.5l6.4 2.3v4.4c0 3.4-3 5.4-6.4 6.6-3.4-1.2-6.4-3.2-6.4-6.6v-4.4z" fill="#06101f" stroke="#f8fafc" stroke-width="1"/><path d="M24 37.6v4" stroke="${c}" stroke-width="1.8" stroke-linecap="round"/><circle cx="24" cy="44" r=".95" fill="${c}"/>`,
  sysadmin: (c) => `<path d="M24 10.5V4.5" stroke="#f8fafc" stroke-width="1.6"/><circle cx="24" cy="4" r="2.3" fill="${c}"/><path d="M20 3.4a5 5 0 0 0 0 5M28 3.4a5 5 0 0 1 0 5" stroke="${c}" stroke-width="1" fill="none"/><path d="M19.2 45l7.6-7.6M28.6 35.6a3.2 3.2 0 1 0 2.2 2.2l-1.8 1.8-1.6-.6-.6-1.6z" stroke="#06101f" stroke-width="2.1" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`,
  pm: (c) => `<path d="M19 33.5l5 3.2 5-3.2" stroke="#f8fafc" stroke-width="1.6" fill="none"/><path d="M22.4 36.6h3.2l-.9 2.8 1.8 7-2.5 2.6-2.5-2.6 1.8-7z" fill="#06101f" stroke="${c}" stroke-width=".9" stroke-linejoin="round"/><rect x="33" y="38.5" width="10" height="7.5" rx="1.4" fill="#0f172a" stroke="#f8fafc" stroke-width="1"/><path d="M36 38.5v-1.6h4v1.6" stroke="#f8fafc" stroke-width="1" fill="none"/>`,
  ingestion: (c) => `<path d="M33 11.5a8 8 0 0 1 6 6M33 6.8a12.8 12.8 0 0 1 10.6 10.6" stroke="${c}" stroke-width="1.8" fill="none" stroke-linecap="round"/><circle cx="33" cy="17.6" r="2" fill="${c}"/><path d="M15 40h18M18 44h12" stroke="#06101f" stroke-width="2" stroke-linecap="round"/><path d="M24 36v-4" stroke="#06101f" stroke-width="2"/>`,
};

function portraitSvg(name) {
  const meta = agentMeta(name);
  const color = meta.color || "#94a3b8";
  const accessory = (ACCESSORY[meta.icon] || ACCESSORY.developer)(color);
  return `<svg class="wf-portrait" viewBox="0 0 48 48" aria-hidden="true">
    <circle cx="24" cy="24" r="23" fill="url(#wf-pbg)"/>
    <circle cx="24" cy="24" r="23" fill="${color}" fill-opacity=".16"/>
    <path d="M6.5 47c1.4-9.6 8.6-14.4 17.5-14.4S40.1 37.4 41.5 47z" fill="${color}"/>
    <path d="M6.5 47c1.4-9.6 8.6-14.4 17.5-14.4S40.1 37.4 41.5 47z" fill="url(#wf-pshade)"/>
    <rect x="20.6" y="27.4" width="6.8" height="6.4" rx="2.2" fill="#94a3b8"/>
    <ellipse cx="24" cy="20.4" rx="9.2" ry="10.2" fill="#dbe4ee"/>
    <path d="M15.2 18.8a9 9 0 0 1 17.6 0" fill="#f8fafc" fill-opacity=".6"/>
    <rect x="15.6" y="17.6" width="16.8" height="6.4" rx="3.2" fill="#0b1220"/>
    <rect x="16.8" y="18.7" width="14.4" height="4.2" rx="2.1" fill="${color}"/>
    <rect x="18" y="19.3" width="5" height="1.3" rx=".65" fill="#fff" fill-opacity=".7"/>
    ${accessory}
  </svg>`;
}

function avatar(name, size = "sm") {
  const meta = agentMeta(name);
  const status = statusOf(name);
  return `<span class="wf-ava wf-ava-${size} is-${status}" data-agent="${esc(name)}" style="--ac:${meta.color}" title="${esc(name)}: ${esc(STATUS_TEXT[status] || status)}">${portraitSvg(name)}</span>`;
}

function nodeIcon(node) {
  const kind = kindOf(node);
  if (kind === "stage") return stageBadge(node);
  if (node.icon === "agent" && node.agents?.length) return `<span class="wf-icon-portrait">${portraitSvg(node.agents[0])}</span>`;
  return glyph(node.icon, kind);
}

const UI_ICON = {
  plus: `<path d="M12 5v14M5 12h14"/>`,
  minus: `<path d="M5 12h14"/>`,
  fit: `<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/><rect x="8.5" y="8.5" width="7" height="7" rx="1.2"/>`,
  edit: `<path d="M12 3v18M3 12h18M12 3l-2.5 2.5M12 3l2.5 2.5M12 21l-2.5-2.5M12 21l2.5-2.5M3 12l2.5-2.5M3 12l2.5 2.5M21 12l-2.5-2.5M21 12l-2.5 2.5"/>`,
  save: `<path d="M5 4h11l3 3v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/>`,
  reset: `<path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4v4.5h4.5"/>`,
  legend: `<rect x="4" y="5" width="5" height="5" rx="1"/><rect x="4" y="14" width="5" height="5" rx="1"/><path d="M12 7.5h8M12 16.5h8"/>`,
  close: `<path d="M6 6l12 12M18 6L6 18"/>`,
  up: `<path d="M6 15l6-6 6 6"/>`,
  down: `<path d="M6 9l6 6 6-6"/>`,
  warn: `<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5"/><circle cx="12" cy="17.3" r=".6"/>`,
  sliders: `<path d="M5 6h14M5 12h14M5 18h14"/><circle cx="9" cy="6" r="2.2"/><circle cx="15" cy="12" r="2.2"/><circle cx="8" cy="18" r="2.2"/>`,
  grip: `<circle cx="9" cy="6" r="1.2"/><circle cx="15" cy="6" r="1.2"/><circle cx="9" cy="12" r="1.2"/><circle cx="15" cy="12" r="1.2"/><circle cx="9" cy="18" r="1.2"/><circle cx="15" cy="18" r="1.2"/>`,
  lock: `<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>`,
  chart: `<path d="M4 19h16"/><path d="M6 15l4-5 3 3 5-7"/>`,
  bolt: `<path d="M13 3L5 13.5h6L10 21l9-11h-6z"/>`,
  file: `<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/>`,
  code: `<path d="M9 7l-5 5 5 5M15 7l5 5-5 5"/>`,
  add: `<path d="M12 5v14M5 12h14"/>`,
};

function uiIcon(name) {
  return `<svg class="wf-ui-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${UI_ICON[name] || ""}</svg>`;
}

function defsSvg() {
  const kinds = Object.entries(KINDS).map(([kind, meta]) => `
      <linearGradient id="wf-gk-${kind}" x1="0" y1="0" x2=".35" y2="1">
        <stop offset="0" stop-color="${mix(meta.color, "#ffffff", 0.45)}"/>
        <stop offset=".55" stop-color="${meta.color}"/>
        <stop offset="1" stop-color="${mix(meta.color, "#000000", 0.35)}"/>
      </linearGradient>
      <linearGradient id="wf-gs-${kind}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="${meta.color}" stop-opacity=".42"/>
        <stop offset="1" stop-color="${meta.color}" stop-opacity=".07"/>
      </linearGradient>`).join("");
  const edges = Object.entries(EDGE_KINDS).map(([kind, meta]) => `
      <radialGradient id="wf-spark-${kind}">
        <stop offset="0" stop-color="#ffffff" stop-opacity=".95"/>
        <stop offset=".32" stop-color="${meta.color}" stop-opacity=".85"/>
        <stop offset="1" stop-color="${meta.color}" stop-opacity="0"/>
      </radialGradient>
      <marker id="wf-arrow-${kind}" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="13" markerHeight="13" markerUnits="userSpaceOnUse" orient="auto-start-reverse">
        <path d="M1 1.5L11 6 1 10.5 3.6 6z" fill="${meta.color}"/>
      </marker>`).join("");
  return `<svg class="wf-defs" width="0" height="0" aria-hidden="true" focusable="false"><defs>
      ${kinds}${edges}
      <radialGradient id="wf-pbg" cx=".5" cy=".35" r=".75">
        <stop offset="0" stop-color="#1e293b"/><stop offset="1" stop-color="#020617"/>
      </radialGradient>
      <linearGradient id="wf-pshade" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="#ffffff" stop-opacity=".28"/><stop offset=".45" stop-color="#ffffff" stop-opacity="0"/><stop offset="1" stop-color="#000000" stop-opacity=".35"/>
      </linearGradient>
    </defs></svg>`;
}

// ---------- page skeleton ----------

function skeleton() {
  return `${defsSvg()}
  <header class="wf-hud">
    <div class="wf-hud-id">
      <span class="wf-emblem">${glyph("mission", "mission")}</span>
      <div class="wf-hud-titles">
        <p class="wf-kicker">Research workflow Design</p>
        <h2 class="wf-hud-goal-title" data-role="goal-title">Mission</h2>
      </div>
    </div>
    <div class="wf-objectives" data-role="objectives" aria-label="Acceptance goal"></div>
  </header>
  <div class="wf-banner" data-role="banner" hidden>
    ${uiIcon("lock")}
    <p title="The settings, folder counts, queue editor and change list need the new desk code. Until then the settings show the code values, read-only, and the stage counts come from the proving ledger."><strong>Restart the desk to edit settings.</strong> Until then the settings show the code values, read-only, and the stage counts come from the proving ledger.</p>
  </div>
  <div class="wf-stage" data-role="stage">
    <div class="wf-viewport" data-role="viewport" tabindex="0" aria-label="Research workflow map. Drag to pan, scroll to zoom.">
      <div class="wf-world" data-role="world">
        <div class="wf-grid"></div>
        <div class="wf-lanes" data-role="lanes"></div>
        <svg class="wf-edges" data-role="edges" aria-hidden="true"><g data-role="edge-paths"></g><g data-role="particles" class="wf-particles"></g></svg>
        <div class="wf-nodes" data-role="nodes"></div>
        <svg class="wf-edge-labels" data-role="edge-labels" aria-hidden="true"></svg>
      </div>
    </div>
    <div class="wf-toolbar" role="toolbar" aria-label="Map controls">
      <div class="wf-tool-group">
        <button type="button" class="wf-tool" data-act="zoom-out" title="Zoom out (-)">${uiIcon("minus")}</button>
        <span class="wf-zoom" data-role="zoom">50%</span>
        <button type="button" class="wf-tool" data-act="zoom-in" title="Zoom in (+)">${uiIcon("plus")}</button>
        <button type="button" class="wf-tool" data-act="fit" title="Fit to screen (0)">${uiIcon("fit")}<span>Fit</span></button>
      </div>
      <div class="wf-tool-group">
        <button type="button" class="wf-tool wf-tool-switch" data-act="edit" aria-pressed="false" title="Drag the cards to arrange the map">
          <span class="wf-mini-switch"><span></span></span><span>Arrange</span>
        </button>
        <button type="button" class="wf-tool" data-act="save-layout" data-edit-only hidden disabled>${uiIcon("save")}<span>Save layout</span></button>
        <button type="button" class="wf-tool" data-act="reset-layout" data-edit-only hidden>${uiIcon("reset")}<span>Reset layout</span></button>
      </div>
      <div class="wf-tool-group">
        <button type="button" class="wf-tool" data-act="legend" aria-pressed="false" title="Legend">${uiIcon("legend")}<span>Legend</span></button>
      </div>
      <span class="wf-layout-note" data-role="layout-note"></span>
      <div class="wf-tool-group wf-hud-status" aria-label="Live status">
        <div class="wf-research" data-role="research"><span class="wf-led"></span><span class="wf-research-text">Research</span></div>
        <div class="wf-engine-leds" data-role="engine-leds" aria-label="Engines"></div>
        <div class="wf-xp" data-role="xp" title="Share of the recent changes that moved toward the goal">
          <span class="wf-xp-label">Toward the goal</span>
          <span class="wf-xp-bar"><span class="wf-xp-fill"></span></span>
          <span class="wf-xp-value">—</span>
        </div>
      </div>
    </div>
    <aside class="wf-legend" data-role="legend" hidden aria-label="Legend"></aside>
    <div class="wf-minimap" data-role="minimap" aria-label="Minimap"><svg data-role="minimap-svg"></svg></div>
    <aside class="wf-drawer" data-role="drawer" aria-label="Step details" hidden></aside>
    <section class="wf-impact" data-role="impact" aria-label="Change impact">
      <div class="wf-impact-bar">
        <button type="button" class="wf-impact-toggle" data-act="impact" aria-expanded="false">
          <span class="wf-impact-caret">${uiIcon("up")}</span>
          <span class="wf-impact-title">${uiIcon("chart")}Change impact</span>
        </button>
        <div class="wf-impact-mini" data-role="impact-mini"></div>
      </div>
      <div class="wf-impact-body" data-role="impact-body" hidden>
        <div class="wf-impact-tools">
          <div class="wf-filters" data-role="filters" role="group" aria-label="Show"></div>
          <div class="wf-sumbars" data-role="sumbars"></div>
        </div>
        <p class="wf-impact-note" data-role="impact-note"></p>
        <div class="wf-impact-scroll"><table class="wf-impact-table"><thead><tr>
          <th>When</th><th>Script</th><th>Chart</th><th>Stage</th><th>Change</th><th>Δ Profit</th><th>Δ Drawdown</th><th>Δ Trades</th><th>A week</th><th>Misses</th><th>Verdict</th>
        </tr></thead><tbody data-role="impact-rows"></tbody></table></div>
      </div>
    </section>
    <div class="wf-toast" data-role="toast" role="status" aria-live="polite"></div>
  </div>`;
}

function cardHtml(node) {
  const kind = kindOf(node);
  const meta = KINDS[kind];
  const agents = Array.isArray(node.agents) ? node.agents : [];
  const showEngines = kind === "engine" || kind === "stage";
  const portraits = agents.slice(0, 4).map((name) => avatar(name)).join("");
  const more = agents.length > 4 ? `<span class="wf-more">+${agents.length - 4}</span>` : "";
  const knobCount = knobsFor(node).length;
  const issues = (node.issues || []).length;
  const badge = kind !== "stage" && node.badge ? `<span class="wf-badge" data-role="badge">${esc(node.badge)}</span>` : "";
  return `<div class="wf-node wf-k-${kind}" data-id="${esc(node.id)}" style="--c:${meta.color}" tabindex="0" role="button" aria-label="${esc(node.title)}: ${esc(meta.label)}">
    <span class="wf-node-aura"></span>
    <span class="wf-counter" data-role="counter" hidden></span>
    <span class="wf-grip">${uiIcon("grip")}</span>
    <div class="wf-node-head">
      <div class="wf-icon">${nodeIcon(node)}</div>
      <div class="wf-node-titles">
        <span class="wf-kind">${esc(meta.label)}${badge}</span>
        <h3>${esc(node.title)}</h3>
        ${kind === "stage" ? `<span class="wf-stage-stats" data-role="stats"></span>` : ""}
      </div>
    </div>
    <p class="wf-summary">${esc(node.summary)}</p>
    <div class="wf-node-foot">
      ${showEngines ? `<div class="wf-chips" data-role="chips"></div>` : `<div class="wf-ava-row">${portraits}${more}</div>`}
      <div class="wf-foot-right">
        <span class="wf-flag wf-flag-live" data-role="live" hidden></span>
        ${knobCount ? `<span class="wf-flag wf-flag-knobs" data-role="knob-flag" title="${knobCount} setting${knobCount === 1 ? "" : "s"} you can change">${uiIcon("sliders")}<b>${knobCount}</b></span>` : ""}
        ${issues ? `<span class="wf-flag wf-flag-warn" title="${issues} problem${issues === 1 ? "" : "s"} against the goal">${uiIcon("warn")}<b>${issues}</b></span>` : ""}
      </div>
    </div>
  </div>`;
}

// ---------- build ----------

async function ensureBuilt() {
  if (ui.built) return true;
  if (!ui.loading) {
    ui.loading = (async () => {
      const response = await fetch(CATALOG_URL, { cache: "no-cache" });
      if (!response.ok) throw new Error(`The workflow catalog did not load (${response.status}).`);
      build(await response.json());
      return true;
    })().catch((error) => {
      ui.loading = null;
      ui.failed = error.message || String(error);
      if (ui.section) ui.section.innerHTML = `<div class="wf-loadfail"><h2>Research workflow Design</h2><p>${esc(ui.failed)}</p></div>`;
      return false;
    });
  }
  return ui.loading;
}

function build(catalog) {
  const section = ui.section;
  ui.catalog = catalog;
  ui.nodes = Array.isArray(catalog.nodes) ? catalog.nodes : [];
  ui.byId = new Map(ui.nodes.map((node) => [node.id, node]));
  ui.edges = (Array.isArray(catalog.edges) ? catalog.edges : []).filter((edge) => ui.byId.has(edge.from) && ui.byId.has(edge.to));
  ui.base = Object.fromEntries(ui.nodes.map((node) => [node.id, [Number(node.x) || 0, Number(node.y) || 0]]));
  ui.pos = Object.fromEntries(Object.entries(ui.base).map(([id, place]) => [id, [...place]]));
  const local = remember("layout", null);
  if (local && typeof local === "object" && Object.keys(local).length) {
    applyLayout(local);
    ui.layoutSource = "browser";
  }
  ui.impactOpen = Boolean(remember("impactOpen", false));
  ui.legendOpen = Boolean(remember("legendOpen", false));
  ui.hidden = new Set(remember("hiddenEdges", []));
  section.classList.add("wf-page");
  section.innerHTML = skeleton();
  const pick = (role) => section.querySelector(`[data-role="${role}"]`);
  for (const role of ["objectives", "goal-title", "research", "engine-leds", "xp", "banner", "stage", "viewport", "world", "lanes", "edges",
    "edge-paths", "particles", "nodes", "edge-labels", "zoom", "legend", "minimap", "minimap-svg", "drawer", "impact", "impact-mini",
    "impact-body", "filters", "sumbars", "impact-note", "impact-rows", "toast", "layout-note"]) {
    ui.els[role] = pick(role);
  }
  buildLanes();
  buildNodes();
  buildEdges();
  buildLegend();
  buildMinimap();
  bindEvents();
  renderImpactShell();
  setImpactOpen(ui.impactOpen, false);
  setLegendOpen(ui.legendOpen);
  renderGoalTitle();
  ui.built = true;
}

function worldSize() {
  let width = Number(ui.catalog?.world?.width) || 2800;
  let height = Number(ui.catalog?.world?.height) || 1600;
  for (const [x, y] of Object.values(ui.pos)) {
    width = Math.max(width, x + CARD_W + 80);
    height = Math.max(height, y + CARD_H + 80);
  }
  ui.world = { width, height };
  return ui.world;
}

function buildLanes() {
  const { width } = worldSize();
  const lanes = Array.isArray(ui.catalog.lanes) ? ui.catalog.lanes : [];
  ui.els.lanes.innerHTML = lanes.map((lane, index) => `
    <div class="wf-lane" style="top:${Number(lane.y) || 0}px;height:${Number(lane.height) || 290}px;width:${width}px;--lc:${esc(lane.color || "#38bdf8")}">
      <span class="wf-lane-title"><b>${String(index + 1).padStart(2, "0")}</b>${esc(lane.title)}</span>
    </div>`).join("");
  sizeWorld();
}

function sizeWorld() {
  const { width, height } = worldSize();
  ui.els.world.style.width = `${width}px`;
  ui.els.world.style.height = `${height}px`;
  for (const svg of [ui.els.edges, ui.els["edge-labels"]]) {
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  }
  ui.els.lanes.querySelectorAll(".wf-lane").forEach((lane) => { lane.style.width = `${width}px`; });
}

function buildNodes() {
  ui.els.nodes.innerHTML = ui.nodes.map((node) => cardHtml(node)).join("");
  ui.cards = new Map([...ui.els.nodes.querySelectorAll(".wf-node")].map((card) => [card.dataset.id, card]));
  for (const node of ui.nodes) placeCard(node.id);
}

function placeCard(id) {
  const card = ui.cards?.get(id);
  const place = ui.pos[id];
  if (!card || !place) return;
  card.style.transform = `translate(${place[0]}px, ${place[1]}px)`;
}

function buildEdges() {
  const paths = ui.edges.map((edge, index) => {
    const kind = EDGE_KINDS[edge.kind] ? edge.kind : "main";
    return `<g class="wf-edge wf-e-${kind}" data-edge="${index}" data-kind="${kind}">
      <path class="wf-edge-glow"/>
      <path class="wf-edge-core" id="wf-e-${index}" marker-end="url(#wf-arrow-${kind})"/>
      <path class="wf-edge-flow"/>
    </g>`;
  }).join("");
  ui.els["edge-paths"].innerHTML = paths;
  ui.els["edge-labels"].innerHTML = ui.edges.map((edge, index) => {
    if (!edge.label) return "";
    const kind = EDGE_KINDS[edge.kind] ? edge.kind : "main";
    return `<g class="wf-elabel wf-e-${kind}" data-edge="${index}"><rect rx="8" ry="8" height="18"/><text dy="12.6">${esc(edge.label)}</text></g>`;
  }).join("");
  ui.edgeEls = [...ui.els["edge-paths"].querySelectorAll(".wf-edge")];
  ui.labelEls = new Map([...ui.els["edge-labels"].querySelectorAll(".wf-elabel")].map((el) => [Number(el.dataset.edge), el]));
  routeEdges();
  applyEdgeFilter();
}

function sidesFor(edge, a, b) {
  const ax = a[0] + CARD_W / 2;
  const ay = a[1] + CARD_H / 2;
  const bx = b[0] + CARD_W / 2;
  const by = b[1] + CARD_H / 2;
  const dx = bx - ax;
  const dy = by - ay;
  if (Math.abs(dy) < CARD_H * 0.6) {
    const reverse = ui.edges.some((other) => other !== edge && other.from === edge.to && other.to === edge.from);
    const adjacent = Math.abs(dx) <= CARD_W + 140;
    if (adjacent && !(reverse && edge.kind === "loop")) return dx >= 0 ? ["right", "left"] : ["left", "right"];
    return edge.kind === "fail" ? ["bottom", "bottom"] : ["top", "top"];
  }
  return dy > 0 ? ["bottom", "top"] : ["top", "bottom"];
}

const NORMAL = { left: [-1, 0], right: [1, 0], top: [0, -1], bottom: [0, 1] };

function routeEdges() {
  const ends = new Map();
  const plans = ui.edges.map((edge) => {
    const a = ui.pos[edge.from];
    const b = ui.pos[edge.to];
    const [from, to] = sidesFor(edge, a, b);
    return { edge, a, b, from, to };
  });
  plans.forEach((plan, index) => {
    for (const [end, id, side, other] of [["s", plan.edge.from, plan.from, plan.b], ["t", plan.edge.to, plan.to, plan.a]]) {
      const key = `${id}:${side}`;
      if (!ends.has(key)) ends.set(key, []);
      const along = side === "left" || side === "right" ? other[1] : other[0];
      ends.get(key).push({ index, end, along });
    }
  });
  const point = {};
  const placed = [];
  for (const [key, list] of ends) {
    const [id, side] = key.split(":");
    const [x, y] = ui.pos[id];
    list.sort((p, q) => p.along - q.along || p.index - q.index);
    list.forEach((item, order) => {
      const share = (order + 1) / (list.length + 1);
      let at;
      if (side === "top") at = [x + CARD_W * (0.16 + 0.68 * share), y];
      else if (side === "bottom") at = [x + CARD_W * (0.16 + 0.68 * share), y + CARD_H];
      else if (side === "left") at = [x, y + CARD_H * (0.22 + 0.56 * share)];
      else at = [x + CARD_W, y + CARD_H * (0.22 + 0.56 * share)];
      point[`${item.index}:${item.end}`] = at;
    });
  }
  plans.forEach((plan, index) => {
    const start = point[`${index}:s`];
    const end = point[`${index}:t`];
    const n0 = NORMAL[plan.from];
    const n1 = NORMAL[plan.to];
    let reach;
    if (plan.from === plan.to) {
      reach = clamp(64 + Math.abs(end[0] - start[0]) * 0.025, 64, 96);
    } else if (plan.from === "left" || plan.from === "right") {
      reach = clamp(Math.abs(end[0] - start[0]) * 0.5, 24, 260);
    } else {
      reach = clamp(Math.abs(end[1] - start[1]) * 0.5, 30, 300);
    }
    const c1 = [start[0] + n0[0] * reach, start[1] + n0[1] * reach];
    const tip = [end[0] + n1[0] * 3, end[1] + n1[1] * 3];
    const c2 = [tip[0] + n1[0] * reach, tip[1] + n1[1] * reach];
    const d = `M${start[0].toFixed(1)} ${start[1].toFixed(1)}C${c1[0].toFixed(1)} ${c1[1].toFixed(1)} ${c2[0].toFixed(1)} ${c2[1].toFixed(1)} ${tip[0].toFixed(1)} ${tip[1].toFixed(1)}`;
    const group = ui.edgeEls[index];
    group.querySelectorAll("path").forEach((path) => path.setAttribute("d", d));
    const label = ui.labelEls.get(index);
    if (label) {
      const text = label.querySelector("text");
      const width = Math.max(30, String(plan.edge.label).length * 6.1 + 18);
      const mid = labelPoint(start, c1, c2, tip, width, placed);
      placed.push([mid[0] - width / 2, mid[1] - 10, mid[0] + width / 2, mid[1] + 10]);
      const rect = label.querySelector("rect");
      rect.setAttribute("x", (-width / 2).toFixed(1));
      rect.setAttribute("y", "-9");
      rect.setAttribute("width", width.toFixed(1));
      text.setAttribute("x", "0");
      text.setAttribute("y", "-9");
      label.setAttribute("transform", `translate(${mid[0].toFixed(1)} ${mid[1].toFixed(1)})`);
    }
  });
}

// A label sits on its curve at the point nearest the middle that covers no card and no other label.
function labelPoint(start, c1, c2, tip, width, placed) {
  const half = width / 2 + 2;
  const blocked = ([x, y]) => {
    for (const [nx, ny] of Object.values(ui.pos)) {
      if (x + half > nx && x - half < nx + CARD_W && y + 12 > ny && y - 12 < ny + CARD_H) return true;
    }
    return placed.some(([x0, y0, x1, y1]) => x + half > x0 && x - half < x1 && y + 13 > y0 && y - 13 < y1);
  };
  for (const t of [0.5, 0.44, 0.56, 0.38, 0.62, 0.32, 0.68, 0.26, 0.74, 0.2, 0.8, 0.14, 0.86]) {
    const at = bezier(start, c1, c2, tip, t);
    if (!blocked(at)) return at;
  }
  return bezier(start, c1, c2, tip, 0.5);
}

function bezier(p0, p1, p2, p3, t) {
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const d = t * t * t;
  return [a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]];
}

function applyEdgeFilter() {
  ui.edgeEls?.forEach((group, index) => {
    const hidden = ui.hidden.has(ui.edges[index].kind);
    group.classList.toggle("is-off", hidden);
    ui.labelEls.get(index)?.classList.toggle("is-off", hidden);
  });
  ui.particleKey = "";
  renderParticles();
}

function renderParticles() {
  const layer = ui.els.particles;
  if (!layer) return;
  if (reduceMotion.matches || !ui.visible) {
    if (layer.childElementCount) layer.replaceChildren();
    ui.particleKey = "";
    return;
  }
  const plan = ui.edges.map((edge) => {
    if (ui.hidden.has(edge.kind)) return 0;
    const ambient = edge.kind === "main" || edge.kind === "creation" ? 1 : 0;
    const load = edge.kind === "rule" || edge.kind === "golden" || edge.kind === "locked" ? 0 : ui.loads[edge.to] || 0;
    return Math.min(5, ambient + load);
  });
  const key = plan.join(",");
  if (key === ui.particleKey) return;
  ui.particleKey = key;
  let markup = "";
  plan.forEach((count, index) => {
    if (!count) return;
    const kind = EDGE_KINDS[ui.edges[index].kind] ? ui.edges[index].kind : "main";
    const path = ui.edgeEls[index].querySelector(".wf-edge-core");
    let length = 400;
    try {
      length = path.getTotalLength() || 400;
    } catch (error) {
      length = 400;
    }
    const duration = clamp(length / 105, 3.2, 16);
    const busy = (ui.loads[ui.edges[index].to] || 0) > 0;
    for (let at = 0; at < count; at += 1) {
      const begin = -((duration * at) / count + (index % 7) * 0.37);
      markup += `<g class="wf-spark${busy ? " is-busy" : ""}"><circle r="${busy ? 10 : 8}" fill="url(#wf-spark-${kind})"/>
        <rect x="-3.4" y="-4.3" width="6.8" height="8.6" rx="1.5" fill="#f8fafc" stroke="${EDGE_KINDS[kind].color}" stroke-width="1.1"/>
        <path d="M-1.8 -1.6h3.6M-1.8 .6h3.6M-1.8 2.6h2.2" stroke="#0b1220" stroke-width=".8"/>
        <animateMotion dur="${duration.toFixed(2)}s" repeatCount="indefinite" begin="${begin.toFixed(2)}s" rotate="0"><mpath href="#wf-e-${index}"/></animateMotion></g>`;
    }
  });
  layer.innerHTML = markup;
}

function buildLegend() {
  const kinds = Object.entries(KINDS).map(([kind, meta]) => {
    const sample = ui.nodes.find((node) => kindOf(node) === kind);
    const icon = kind === "stage" ? stageBadge({ badge: "7D" }) : glyph(sample?.icon === "agent" ? "mini_agent" : sample?.icon || "files", kind);
    return `<li style="--c:${meta.color}"><span class="wf-leg-icon">${icon}</span><span><b>${esc(meta.label)}</b><small>${esc(meta.note)}</small></span></li>`;
  }).join("");
  const edges = Object.entries(EDGE_KINDS).map(([kind, meta]) => `
    <li><button type="button" class="wf-leg-edge${ui.hidden.has(kind) ? " is-off" : ""}" data-act="edge-kind" data-kind="${kind}" aria-pressed="${ui.hidden.has(kind) ? "false" : "true"}" title="Show or hide these lines">
      <svg viewBox="0 0 54 12" class="wf-leg-line wf-e-${kind}" aria-hidden="true"><path class="wf-edge-core" d="M2 6H46" marker-end="url(#wf-arrow-${kind})"/></svg>
      <span>${esc(meta.label)}</span></button></li>`).join("");
  const rings = ["working", "pending", "done", "idle"].map((status) => `
    <li><span class="wf-ava wf-ava-xs is-${status}" style="--ac:#94a3b8"><svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="22" fill="#1e293b"/></svg></span><span>${esc(STATUS_TEXT[status])}</span></li>`).join("");
  ui.els.legend.innerHTML = `
    <header><h4>Legend</h4><button type="button" class="wf-icon-btn" data-act="legend" aria-label="Close the legend">${uiIcon("close")}</button></header>
    <div class="wf-leg-cols">
      <section><h5>Cards</h5><ul class="wf-leg-kinds">${kinds}</ul></section>
      <section><h5>Lines <small>click to show or hide</small></h5><ul class="wf-leg-edges">${edges}</ul>
        <h5>Agent light</h5><ul class="wf-leg-rings">${rings}</ul>
        <h5>On the cards</h5>
        <ul class="wf-leg-misc">
          <li><span class="wf-flag wf-flag-warn">${uiIcon("warn")}<b>3</b></span><span>Problems against the goal</span></li>
          <li><span class="wf-flag wf-flag-knobs">${uiIcon("sliders")}<b>2</b></span><span>Settings you can change</span></li>
          <li><span class="wf-chip is-busy">E2</span><span>Engine running a backtest here</span></li>
          <li><span class="wf-leg-spark"></span><span>A script moving; more when more are at that step</span></li>
        </ul>
      </section>
    </div>`;
}

function buildMinimap() {
  const svg = ui.els["minimap-svg"];
  const { width, height } = worldSize();
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const lanes = (ui.catalog.lanes || []).map((lane) => `<rect x="0" y="${lane.y}" width="${width}" height="${lane.height}" rx="30" fill="${esc(lane.color)}" fill-opacity=".09" stroke="${esc(lane.color)}" stroke-opacity=".35" stroke-width="1.2" vector-effect="non-scaling-stroke"/>`).join("");
  const nodes = ui.nodes.map((node) => `<rect class="wf-mm-node" data-id="${esc(node.id)}" width="${CARD_W - 50}" height="${CARD_H - 50}" rx="22" fill="${colorOf(node)}" fill-opacity=".5"/>`).join("");
  svg.innerHTML = `<rect width="${width}" height="${height}" fill="#020617" fill-opacity=".6"/>${lanes}${nodes}<rect class="wf-mm-view" data-role="mm-view" rx="18"/>`;
  ui.mmNodes = new Map([...svg.querySelectorAll(".wf-mm-node")].map((rect) => [rect.dataset.id, rect]));
  ui.mmView = svg.querySelector('[data-role="mm-view"]');
  placeMinimap();
}

function placeMinimap() {
  if (!ui.mmNodes) return;
  for (const [id, rect] of ui.mmNodes) {
    const place = ui.pos[id];
    rect.setAttribute("x", place[0] + 25);
    rect.setAttribute("y", place[1] + 25);
    rect.classList.toggle("is-sel", id === ui.selected);
    rect.classList.toggle("is-busy", (ui.loads[id] || 0) > 0);
  }
  updateMinimapView();
}

function updateMinimapView() {
  if (!ui.mmView || !ui.els.viewport) return;
  const { width, height } = viewSize();
  const { x, y, k } = ui.cam;
  ui.mmView.setAttribute("x", (-x / k).toFixed(1));
  ui.mmView.setAttribute("y", (-y / k).toFixed(1));
  ui.mmView.setAttribute("width", Math.max(10, width / k).toFixed(1));
  ui.mmView.setAttribute("height", Math.max(10, height / k).toFixed(1));
}

// ---------- camera ----------

function viewSize() {
  const box = ui.els.viewport;
  return { width: box?.clientWidth || 0, height: box?.clientHeight || 0 };
}

function applyCamera() {
  const { x, y, k } = ui.cam;
  ui.els.world.style.transform = `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px) scale(${k.toFixed(4)})`;
  ui.els.zoom.textContent = `${Math.round(k * 100)}%`;
  ui.els.world.classList.toggle("is-far", k < 0.42);
  ui.els.world.classList.toggle("is-tiny", k < 0.26);
  updateMinimapView();
}

function zoomAt(px, py, next) {
  const k = clamp(next, ZOOM_MIN, ZOOM_MAX);
  const wx = (px - ui.cam.x) / ui.cam.k;
  const wy = (py - ui.cam.y) / ui.cam.k;
  ui.cam = { x: px - wx * k, y: py - wy * k, k };
  ui.userMoved = true;
  applyCamera();
}

function zoomBy(factor) {
  const { width, height } = viewSize();
  zoomAt(width / 2, height / 2, ui.cam.k * factor);
}

function insets() {
  const drawer = ui.selected && !ui.els.drawer.hidden ? Math.min(ui.els.drawer.offsetWidth || 440, viewSize().width * 0.6) : 0;
  const impact = ui.els.impact ? ui.els.impact.offsetHeight : 48;
  return { top: 62, bottom: impact + 18, left: 18, right: drawer + 18 };
}

function fit(animate = false) {
  const { width, height } = viewSize();
  if (!width || !height) return false;
  const places = Object.values(ui.pos);
  if (!places.length) return false;
  const minX = Math.min(...places.map((p) => p[0])) - 40;
  const minY = Math.min(...places.map((p) => p[1])) - 70;
  const maxX = Math.max(...places.map((p) => p[0] + CARD_W)) + 40;
  const maxY = Math.max(...places.map((p) => p[1] + CARD_H)) + 40;
  const pad = insets();
  const room = { w: Math.max(80, width - pad.left - pad.right), h: Math.max(80, height - pad.top - pad.bottom) };
  const k = clamp(Math.min(room.w / (maxX - minX), room.h / (maxY - minY)), ZOOM_MIN, 1.1);
  const target = {
    k,
    x: pad.left + (room.w - (maxX - minX) * k) / 2 - minX * k,
    y: pad.top + (room.h - (maxY - minY) * k) / 2 - minY * k,
  };
  moveCamera(target, animate);
  return true;
}

function moveCamera(target, animate) {
  ui.flight += 1;
  const flight = ui.flight;
  if (!animate || reduceMotion.matches) {
    ui.cam = target;
    applyCamera();
    return;
  }
  const from = { ...ui.cam };
  const started = performance.now();
  const span = 420;
  const step = (now) => {
    if (flight !== ui.flight) return;
    const t = clamp((now - started) / span, 0, 1);
    const ease = 1 - Math.pow(1 - t, 3);
    ui.cam = {
      x: from.x + (target.x - from.x) * ease,
      y: from.y + (target.y - from.y) * ease,
      k: from.k + (target.k - from.k) * ease,
    };
    applyCamera();
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function flyTo(id) {
  const place = ui.pos[id];
  if (!place) return;
  const { width, height } = viewSize();
  const pad = insets();
  const k = clamp(Math.max(ui.cam.k, 0.62), ZOOM_MIN, 1.05);
  const room = { w: width - pad.left - pad.right, h: height - pad.top - pad.bottom };
  moveCamera({
    k,
    x: pad.left + room.w / 2 - (place[0] + CARD_W / 2) * k,
    y: pad.top + room.h / 2 - (place[1] + CARD_H / 2) * k,
  }, true);
  ui.userMoved = true;
}

function centerOn(wx, wy) {
  const { width, height } = viewSize();
  moveCamera({ k: ui.cam.k, x: width / 2 - wx * ui.cam.k, y: height / 2 - wy * ui.cam.k }, false);
  ui.userMoved = true;
}

// ---------- layout ----------

function applyLayout(layout) {
  for (const [id, place] of Object.entries(layout || {})) {
    if (!ui.base[id] || !Array.isArray(place) || place.length !== 2) continue;
    const x = Number(place[0]);
    const y = Number(place[1]);
    if (Number.isFinite(x) && Number.isFinite(y)) ui.pos[id] = [x, y];
  }
}

function movedLayout() {
  const moved = {};
  for (const [id, place] of Object.entries(ui.pos)) {
    const base = ui.base[id];
    if (!base || base[0] !== place[0] || base[1] !== place[1]) moved[id] = [place[0], place[1]];
  }
  return moved;
}

function relayout() {
  for (const node of ui.nodes) placeCard(node.id);
  sizeWorld();
  routeEdges();
  placeMinimap();
  ui.particleKey = "";
  renderParticles();
}

function setLayoutNote(text) {
  ui.layoutNote = text;
  if (ui.els["layout-note"]) ui.els["layout-note"].textContent = text;
}

function updateLayoutButtons() {
  const save = ui.section.querySelector('[data-act="save-layout"]');
  if (save) save.disabled = !ui.layoutDirty;
  const source = { desk: "Layout saved on the desk.", browser: "Layout saved in this browser.", catalog: "" }[ui.layoutSource] || "";
  if (!ui.layoutDirty && ui.edit) setLayoutNote(source || "Drag a card to move it.");
  if (ui.layoutDirty) setLayoutNote("Layout changed. Save it to keep it.");
  if (!ui.edit && !ui.layoutDirty) setLayoutNote("");
}

function setEdit(on) {
  ui.edit = Boolean(on);
  ui.section.classList.toggle("wf-editing", ui.edit);
  const button = ui.section.querySelector('[data-act="edit"]');
  button?.setAttribute("aria-pressed", ui.edit ? "true" : "false");
  ui.section.querySelectorAll("[data-edit-only]").forEach((el) => { el.hidden = !ui.edit; });
  updateLayoutButtons();
}

async function saveLayout() {
  const layout = movedLayout();
  if (editable()) {
    const result = await post(`${API}/layout`, { layout });
    if (!result.ok) {
      toast(result.detail || "The layout did not save.", "bad");
      return;
    }
    keep("layout", undefined);
    ui.layoutSource = Object.keys(layout).length ? "desk" : "catalog";
    if (ui.design) ui.design.layout = result.data?.layout || layout;
    ui.layoutDirty = false;
    toast("Layout saved on the desk.", "good");
  } else {
    keep("layout", layout);
    ui.layoutSource = "browser";
    ui.layoutDirty = false;
    toast("Layout saved in this browser. After the desk restarts, Save layout keeps it on the desk.", "good");
  }
  updateLayoutButtons();
}

async function resetLayout() {
  if (editable()) {
    const result = await post(`${API}/layout`, { layout: {} });
    if (!result.ok) {
      toast(result.detail || "The layout did not reset.", "bad");
      return;
    }
    if (ui.design) ui.design.layout = {};
  }
  keep("layout", undefined);
  ui.pos = Object.fromEntries(Object.entries(ui.base).map(([id, place]) => [id, [...place]]));
  ui.layoutSource = "catalog";
  ui.layoutDirty = false;
  relayout();
  updateLayoutButtons();
  fit(true);
  toast("The map is back to the catalog layout.", "good");
}

// ---------- events ----------

function bindEvents() {
  const viewport = ui.els.viewport;
  viewport.addEventListener("pointerdown", onPointerDown);
  viewport.addEventListener("pointermove", onPointerMove);
  viewport.addEventListener("pointerup", onPointerUp);
  viewport.addEventListener("pointercancel", onPointerUp);
  viewport.addEventListener("wheel", onWheel, { passive: false });
  viewport.addEventListener("pointerover", (event) => {
    const card = event.target.closest?.(".wf-node");
    setHover(card ? card.dataset.id : null);
  });
  viewport.addEventListener("pointerleave", () => setHover(null));
  viewport.addEventListener("keydown", (event) => {
    const card = event.target.closest?.(".wf-node");
    if (card && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      select(card.dataset.id, true);
    }
  });
  viewport.addEventListener("focusin", (event) => {
    const card = event.target.closest?.(".wf-node");
    if (card) setHover(card.dataset.id);
  });
  ui.section.addEventListener("click", onClick);
  ui.section.addEventListener("input", onInput);
  ui.section.addEventListener("change", onChange);
  const minimap = ui.els.minimap;
  let mapDrag = false;
  const mapMove = (event) => {
    const svg = ui.els["minimap-svg"];
    const box = svg.getBoundingClientRect();
    if (!box.width) return;
    // The map keeps its shape inside the box (letterboxed), so map the click through the SVG's own transform.
    const matrix = svg.getScreenCTM && svg.getScreenCTM();
    if (matrix && svg.createSVGPoint) {
      const point = svg.createSVGPoint();
      point.x = event.clientX;
      point.y = event.clientY;
      const at = point.matrixTransform(matrix.inverse());
      centerOn(at.x, at.y);
      return;
    }
    const { width, height } = ui.world;
    centerOn(((event.clientX - box.left) / box.width) * width, ((event.clientY - box.top) / box.height) * height);
  };
  minimap.addEventListener("pointerdown", (event) => {
    mapDrag = true;
    minimap.setPointerCapture(event.pointerId);
    mapMove(event);
  });
  minimap.addEventListener("pointermove", (event) => { if (mapDrag) mapMove(event); });
  minimap.addEventListener("pointerup", () => { mapDrag = false; });
  minimap.addEventListener("pointercancel", () => { mapDrag = false; });
  document.addEventListener("keydown", onKey);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else if (ui.visible) startPolling();
  });
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => {
      if (!ui.visible) return;
      if (!ui.userMoved) fit(false);
      else updateMinimapView();
    }).observe(viewport);
    // The drawer, legend and minimap sit just above the change-impact panel, whatever its height.
    new ResizeObserver(() => {
      ui.section.style.setProperty("--wf-impact-now", `${ui.els.impact.offsetHeight}px`);
    }).observe(ui.els.impact);
  }
  const motion = () => {
    ui.particleKey = "";
    renderParticles();
  };
  if (reduceMotion.addEventListener) reduceMotion.addEventListener("change", motion);
}

function onPointerDown(event) {
  if (event.pointerType === "mouse" && event.button !== 0) return;
  if (event.target.closest(".wf-no-drag")) return;
  const card = event.target.closest(".wf-node");
  const id = card?.dataset.id || null;
  ui.drag = {
    pointer: event.pointerId,
    sx: event.clientX,
    sy: event.clientY,
    cam: { ...ui.cam },
    id,
    start: id ? [...ui.pos[id]] : null,
    moved: false,
  };
  ui.flight += 1;
  ui.els.viewport.setPointerCapture?.(event.pointerId);
}

function onPointerMove(event) {
  const drag = ui.drag;
  if (!drag || drag.pointer !== event.pointerId) return;
  const dx = event.clientX - drag.sx;
  const dy = event.clientY - drag.sy;
  if (!drag.moved && Math.hypot(dx, dy) < 4) return;
  if (!drag.moved) {
    drag.moved = true;
    ui.section.classList.add(ui.edit && drag.id ? "wf-dragging-card" : "wf-panning");
    if (ui.edit && drag.id) ui.cards.get(drag.id)?.classList.add("is-dragging");
  }
  if (ui.edit && drag.id) {
    const x = Math.round((drag.start[0] + dx / ui.cam.k) / SNAP) * SNAP;
    const y = Math.round((drag.start[1] + dy / ui.cam.k) / SNAP) * SNAP;
    const place = ui.pos[drag.id];
    if (place[0] === x && place[1] === y) return;
    ui.pos[drag.id] = [Math.max(0, x), Math.max(0, y)];
    placeCard(drag.id);
    routeEdges();
    placeMinimap();
    ui.layoutDirty = true;
    updateLayoutButtons();
  } else {
    ui.cam = { k: drag.cam.k, x: drag.cam.x + dx, y: drag.cam.y + dy };
    ui.userMoved = true;
    applyCamera();
  }
}

function onPointerUp(event) {
  const drag = ui.drag;
  if (!drag || drag.pointer !== event.pointerId) return;
  ui.drag = null;
  ui.section.classList.remove("wf-panning", "wf-dragging-card");
  if (drag.id) ui.cards.get(drag.id)?.classList.remove("is-dragging");
  if (drag.moved) {
    if (ui.edit && drag.id) {
      sizeWorld();
      buildMinimap();
      ui.particleKey = "";
      renderParticles();
    }
    return;
  }
  if (event.type === "pointercancel") return;
  if (drag.id) select(drag.id, false);
  else select(null);
}

function onWheel(event) {
  event.preventDefault();
  const box = ui.els.viewport.getBoundingClientRect();
  const scale = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 400 : 1;
  const delta = event.deltaY * scale;
  const factor = Math.exp(-delta * (event.ctrlKey ? 0.006 : 0.0016));
  ui.flight += 1;
  zoomAt(event.clientX - box.left, event.clientY - box.top, ui.cam.k * factor);
}

function onKey(event) {
  if (!ui.visible || !ui.built || event.defaultPrevented || event.altKey || event.metaKey || event.ctrlKey) return;
  const target = event.target;
  if (target && (target.isContentEditable || /^(input|textarea|select)$/i.test(target.tagName))) return;
  if (target && target.closest && target.closest(".chat-dock, .modal")) return;
  if (event.key === "+" || event.key === "=") {
    zoomBy(1.2);
  } else if (event.key === "-" || event.key === "_") {
    zoomBy(1 / 1.2);
  } else if (event.key === "0") {
    ui.userMoved = false;
    fit(true);
  } else if (event.key === "Escape") {
    if (ui.selected) select(null);
    else if (ui.edit) setEdit(false);
    else return;
  } else {
    return;
  }
  event.preventDefault();
}

function onClick(event) {
  const actor = event.target.closest("[data-act]");
  if (!actor || !ui.section.contains(actor)) return;
  const act = actor.dataset.act;
  if (act === "zoom-in") zoomBy(1.25);
  else if (act === "zoom-out") zoomBy(1 / 1.25);
  else if (act === "fit") {
    ui.userMoved = false;
    fit(true);
  } else if (act === "edit") setEdit(!ui.edit);
  else if (act === "save-layout") saveLayout();
  else if (act === "reset-layout") resetLayout();
  else if (act === "legend") setLegendOpen(!ui.legendOpen);
  else if (act === "edge-kind") toggleEdgeKind(actor.dataset.kind);
  else if (act === "impact") setImpactOpen(!ui.impactOpen, true);
  else if (act === "filter") {
    ui.filter = actor.dataset.filter || "all";
    renderImpact(true);
  } else if (act === "change-row") {
    const node = DAYS_NODE[Number(actor.dataset.stage)] || "year";
    select(node, true);
  } else if (act === "close") select(null);
  else if (act === "goto") select(actor.dataset.node, true);
  else if (act === "toggle") toggleKnob(actor.dataset.key);
  else if (act === "knob-save") saveKnobs(actor.dataset.node);
  else if (act === "knob-reset") resetKnobs(actor.dataset.node);
  else if (act === "knob-undo") undoKnobs(actor.dataset.node);
  else if (act === "queue-up" || act === "queue-down" || act === "queue-remove" || act === "queue-add") editQueue(act, actor.dataset.name);
  else if (act === "queue-save") saveQueue();
  else if (act === "answer-question") answerQuestion(actor.dataset.qid);
  else if (act === "queue-revert") {
    ui.queueDraft = null;
    ui.queueNote = null;
    renderDrawerPart("queue");
  }
}

function onInput(event) {
  const field = event.target;
  const key = field.dataset?.knob;
  if (!key) return;
  const knob = knobRows().find((row) => row.key === key);
  if (!knob) return;
  // An emptied number box is the owner still typing, not a 0. Number("") would be 0 and save the minimum.
  if (field.type === "number" && (String(field.value).trim() === "" || !Number.isFinite(field.valueAsNumber))) return;
  const value = Number(field.value);
  if (!Number.isFinite(value)) return;
  ui.draft[key] = knob.type === "int" ? Math.round(value) : value;
  syncKnob(key, field);
}

function onChange(event) {
  const field = event.target;
  const key = field.dataset?.knob;
  if (!key || field.type !== "number") return;
  const knob = knobRows().find((row) => row.key === key);
  if (!knob) return;
  const typed = String(field.value).trim();
  let value = typed === "" ? NaN : Number(typed);
  // A box left empty or invalid keeps the value it had: the edit in progress, or the saved value.
  if (!Number.isFinite(value)) value = Number(ui.draft[key] ?? knob.value);
  if (knob.min != null) value = Math.max(Number(knob.min), value);
  if (knob.max != null) value = Math.min(Number(knob.max), value);
  if (knob.type === "int") value = Math.round(value);
  ui.draft[key] = value;
  field.value = String(value);
  syncKnob(key, null);
}

function toggleEdgeKind(kind) {
  if (!kind) return;
  if (ui.hidden.has(kind)) ui.hidden.delete(kind);
  else ui.hidden.add(kind);
  keep("hiddenEdges", [...ui.hidden]);
  ui.els.legend.querySelectorAll(`[data-kind="${kind}"]`).forEach((button) => {
    button.classList.toggle("is-off", ui.hidden.has(kind));
    button.setAttribute("aria-pressed", ui.hidden.has(kind) ? "false" : "true");
  });
  applyEdgeFilter();
}

function setLegendOpen(open) {
  ui.legendOpen = Boolean(open);
  ui.els.legend.hidden = !ui.legendOpen;
  ui.section.querySelectorAll('.wf-toolbar [data-act="legend"]').forEach((button) => button.setAttribute("aria-pressed", ui.legendOpen ? "true" : "false"));
  keep("legendOpen", ui.legendOpen);
}

function setImpactOpen(open, user) {
  ui.impactOpen = Boolean(open);
  ui.els["impact-body"].hidden = !ui.impactOpen;
  ui.els.impact.classList.toggle("is-open", ui.impactOpen);
  ui.section.querySelector('[data-act="impact"]')?.setAttribute("aria-expanded", ui.impactOpen ? "true" : "false");
  ui.section.classList.toggle("wf-impact-open", ui.impactOpen);
  if (user) keep("impactOpen", ui.impactOpen);
  if (ui.impactOpen) renderImpact(true);
}

function setHover(id) {
  if (ui.hover === id) return;
  ui.hover = id;
  highlight();
}

function linkedTo(id) {
  const nodes = new Set([id]);
  const edges = new Set();
  ui.edges.forEach((edge, index) => {
    if (edge.from === id || edge.to === id) {
      edges.add(index);
      nodes.add(edge.from);
      nodes.add(edge.to);
    }
  });
  return { nodes, edges };
}

function highlight() {
  const focus = ui.selected || ui.hover;
  const world = ui.els.world;
  if (!world) return;
  world.classList.toggle("has-focus", Boolean(focus));
  world.classList.toggle("has-sel", Boolean(ui.selected));
  const link = focus ? linkedTo(focus) : { nodes: new Set(), edges: new Set() };
  ui.cards?.forEach((card, id) => {
    card.classList.toggle("is-sel", id === ui.selected);
    card.classList.toggle("is-link", link.nodes.has(id));
  });
  ui.edgeEls?.forEach((group, index) => {
    const hot = link.edges.has(index);
    group.classList.toggle("is-hot", hot);
    ui.labelEls.get(index)?.classList.toggle("is-hot", hot);
  });
}

// ---------- selection and drawer ----------

function select(id, fly = false) {
  const node = id ? ui.byId.get(id) : null;
  ui.selected = node ? node.id : null;
  highlight();
  placeMinimap();
  const drawer = ui.els.drawer;
  if (!node) {
    drawer.hidden = true;
    drawer.innerHTML = "";
    ui.section.classList.remove("wf-has-drawer");
    return;
  }
  drawer.innerHTML = drawerHtml(node);
  drawer.hidden = false;
  drawer.scrollTop = 0;
  drawer.querySelector(".wf-d-scroll")?.scrollTo?.(0, 0);
  ui.section.classList.add("wf-has-drawer");
  ui.queueNote = null;
  for (const part of ["live", "knobs", "queue", "agents", "engines"]) renderDrawerPart(part);
  const card = ui.cards.get(node.id);
  if (card) {
    card.classList.remove("is-ping");
    void card.offsetWidth;
    card.classList.add("is-ping");
  }
  if (fly || coveredByPanels(node.id)) flyTo(node.id);
}

// True when the card would sit under the drawer or off the screen, so selecting it should bring it into view.
function coveredByPanels(id) {
  const place = ui.pos[id];
  if (!place) return false;
  const { width, height } = viewSize();
  const pad = insets();
  const left = ui.cam.x + place[0] * ui.cam.k;
  const top = ui.cam.y + place[1] * ui.cam.k;
  const right = left + CARD_W * ui.cam.k;
  const bottom = top + CARD_H * ui.cam.k;
  return right > width - pad.right + 8 || left < 0 || top < pad.top - 20 || bottom > height - pad.bottom + 8;
}

function listSection(title, items, className, open = true) {
  if (!items || !items.length) return "";
  return `<details class="wf-d-sec" ${open ? "open" : ""}><summary><span>${title}</span><b>${items.length}</b></summary><ul class="${className}">${items.join("")}</ul></details>`;
}

function drawerHtml(node) {
  const kind = kindOf(node);
  const meta = KINDS[kind];
  const lane = (ui.catalog.lanes || []).find((row) => row.id === node.lane);
  const decisions = (node.decisions || []).map((line) => {
    const [when, then] = String(line).split(/\s*->\s*/);
    const target = then ? `<span class="wf-dec-then">${esc(then)}</span>` : "";
    return `<li><span class="wf-dec-when">${esc(when)}</span>${target ? `<span class="wf-dec-arrow">${uiIcon("bolt")}</span>${target}` : ""}</li>`;
  });
  const issues = (node.issues || []).map((line) => `<li><span class="wf-issue-icon">${uiIcon("warn")}</span><p>${esc(line)}</p></li>`);
  const files = (node.files || []).map((line) => `<li><span class="wf-file">${uiIcon("file")}<code>${esc(line)}</code></span></li>`);
  const code = (node.code || []).map((line) => `<li><span class="wf-file wf-code">${uiIcon("code")}<code>${esc(line)}</code></span></li>`);
  const links = ui.edges.filter((edge) => edge.from === node.id || edge.to === node.id).map((edge) => {
    const out = edge.from === node.id;
    const other = ui.byId.get(out ? edge.to : edge.from);
    const kindName = EDGE_KINDS[edge.kind] ? edge.kind : "main";
    return `<li><button type="button" class="wf-link wf-e-${kindName}" data-act="goto" data-node="${esc(other.id)}">
      <span class="wf-link-dir">${out ? "Then" : "From"}</span><span class="wf-link-name">${esc(other.title)}</span>
      ${edge.label ? `<small>${esc(edge.label)}</small>` : ""}</button></li>`;
  });
  const goalNotes = node.id === "goal" && Array.isArray(ui.catalog.goal?.notes)
    ? listSection("How the goal is really checked", ui.catalog.goal.notes.map((line) => `<li>${esc(line)}</li>`), "wf-notes")
    : "";
  return `
    <div class="wf-d-head" style="--c:${meta.color}">
      <div class="wf-d-icon">${nodeIcon(node)}</div>
      <div class="wf-d-titles">
        <p class="wf-d-tags"><span class="wf-tag">${esc(meta.label)}</span>${lane ? `<span class="wf-tag wf-tag-lane" style="--lc:${esc(lane.color)}">${esc(lane.title)}</span>` : ""}${node.badge ? `<span class="wf-tag wf-tag-badge">${esc(node.badge)}</span>` : ""}</p>
        <h3>${esc(node.title)}</h3>
      </div>
      <button type="button" class="wf-icon-btn wf-d-close" data-act="close" aria-label="Close the details">${uiIcon("close")}</button>
    </div>
    <div class="wf-d-scroll">
      <p class="wf-d-summary">${esc(node.summary)}</p>
      <div data-part="live"></div>
      <div data-part="knobs"></div>
      ${node.id === "queue" ? `<div data-part="queue"></div>` : ""}
      ${node.id === "awaiting" ? `<div data-part="questions"></div>` : ""}
      ${listSection("Problems against the goal", issues, "wf-issues")}
      ${goalNotes}
      ${listSection("Where a script goes", decisions, "wf-decisions")}
      <details class="wf-d-sec" open><summary><span>What really happens</span></summary><p class="wf-d-detail">${esc(node.detail)}</p></details>
      <div data-part="agents"></div>
      <div data-part="engines"></div>
      ${listSection("Connected steps", links, "wf-links", false)}
      ${listSection("Files", files, "wf-files", false)}
      ${listSection("Code", code, "wf-files", false)}
    </div>`;
}

function renderDrawerPart(part, force = false) {
  const node = ui.selected ? ui.byId.get(ui.selected) : null;
  const host = node ? ui.els.drawer.querySelector(`[data-part="${part}"]`) : null;
  if (!host) return;
  const active = host.contains(document.activeElement) ? document.activeElement : null;
  // A poll never replaces a slider, number box or answer the owner is using.
  if (!force && active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
  if (!force && part === "questions" && [...host.querySelectorAll("textarea")].some((box) => box.value.trim())) return;
  let html = "";
  if (part === "live") html = liveHtml(node);
  else if (part === "knobs") html = knobsHtml(node);
  else if (part === "queue") html = queueHtml();
  else if (part === "questions") html = questionsHtml();
  else if (part === "agents") html = agentsHtml(node);
  else if (part === "engines") html = enginesHtml(node);
  if (host.dataset.sig === html) return;
  host.dataset.sig = html;
  const open = host.querySelector("details")?.open;
  const focus = active ? { act: active.dataset.act, key: active.dataset.key || active.dataset.knob, name: active.dataset.name } : null;
  host.innerHTML = html;
  if (open === false) host.querySelector("details")?.removeAttribute("open");
  if (focus && focus.act) {
    const again = [...host.querySelectorAll(`[data-act="${focus.act}"]`)]
      .find((el) => (el.dataset.key || el.dataset.knob) === focus.key && el.dataset.name === focus.name);
    if (again && !again.disabled) again.focus({ preventScroll: true });
  }
}

function tile(value, label, tone = "") {
  return `<div class="wf-tile ${tone}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;
}

function liveHtml(node) {
  const tiles = [];
  const ledger = ledgerNow();
  const days = NODE_DAYS[node.id];
  if (days && ledger) {
    const row = ledger[String(days)] || {};
    const judged = (row.passed || 0) + (row.failed || 0);
    tiles.push(tile(row.testing || 0, "testing now", row.testing ? "is-hot" : ""));
    tiles.push(tile(row.passed || 0, "passed", "is-good"));
    tiles.push(tile(row.failed || 0, "failed", row.failed ? "is-bad" : ""));
    tiles.push(tile(row.stopped || 0, "stopped"));
    tiles.push(tile(judged ? `${Math.round(((row.passed || 0) / judged) * 100)}%` : "—", "pass rate"));
    const testing = testingRows().filter((item) => Number(item.stage) === days);
    const list = testing.map((item) => `<li><span class="wf-chip">${esc(item.root || "?")} ${esc(item.timeframe || "")}</span><span class="wf-ellip">${esc(item.file)}</span></li>`).join("");
    const source = ui.api === "ok" ? "" : `<p class="wf-d-src">From the proving ledger (last 100 rows).</p>`;
    return `<section class="wf-d-live"><h4>Live</h4><div class="wf-tiles">${tiles.join("")}</div>${list ? `<ul class="wf-testing">${list}</ul>` : ""}${source}</section>`;
  }
  const live = liveFor(node);
  if (live) tiles.push(tile(live.value, live.label, live.tone ? `is-${live.tone}` : ""));
  if (node.id === "claim" || node.id === "engine4") {
    const rows = node.id === "claim" ? engineRows().filter((engine) => !isCreation(engine)) : engineRows().filter(isCreation);
    const open = rows.filter((engine) => engine.studying).length;
    if (rows.length) tiles.push(tile(open, "scripts open"));
  }
  if (node.id === "goal") {
    const acc = ui.live.book?.strategy_acceptance;
    if (acc) tiles.push(tile(goalName(acc.goal), "steers the search"));
    const week = weeklyPace();
    if (week?.year.use_profit) tiles.push(tile(money(week.year.weekly_profit), "profit a week"));
    if (week?.year.use_trades) tiles.push(tile(plainNumber(week.year.weekly_trades, 1), "trades a week"));
  }
  if (!tiles.length) return "";
  return `<section class="wf-d-live"><h4>Live</h4><div class="wf-tiles">${tiles.join("")}</div></section>`;
}

function knobsHtml(node) {
  const knobs = knobsFor(node);
  if (!knobs.length) return "";
  const locked = !editable();
  const keys = knobs.map((knob) => knob.key);
  const dirty = keys.some((key) => key in ui.draft && ui.draft[key] !== knobRows().find((row) => row.key === key)?.value);
  const changed = knobs.some((knob) => knob.changed);
  const note = ui.knobNote && ui.knobNote.node === node.id ? `<p class="wf-save-note is-${ui.knobNote.tone}">${esc(ui.knobNote.text)}</p>` : "";
  const rows = knobs.map((knob) => knobHtml(knob, locked)).join("");
  const banner = locked
    ? `<div class="wf-lockline">${uiIcon("lock")}<span><b>Restart the desk to edit settings.</b> These are the code values. They become editable once the desk runs the new code.</span></div>`
    : "";
  return `<section class="wf-d-knobs${locked ? " is-locked" : ""}">
    <h4>${uiIcon("sliders")}Settings <small>${locked ? "read-only" : "live on the desk"}</small></h4>
    ${banner}
    <div class="wf-knob-list">${rows}</div>
    ${locked ? "" : `<div class="wf-knob-actions">
      <button type="button" class="wf-btn wf-btn-primary" data-act="knob-save" data-node="${esc(node.id)}" ${dirty ? "" : "disabled"}>${uiIcon("save")}Save to desk</button>
      <button type="button" class="wf-btn" data-act="knob-undo" data-node="${esc(node.id)}" ${dirty ? "" : "disabled"}>Undo edits</button>
      <button type="button" class="wf-btn wf-btn-ghost" data-act="knob-reset" data-node="${esc(node.id)}" ${changed ? "" : "disabled"}>${uiIcon("reset")}Reset to code values</button>
    </div>`}
    ${note}
  </section>`;
}

function knobHtml(knob, locked) {
  const value = knobValue(knob.key);
  const edited = knob.key in ui.draft && ui.draft[knob.key] !== knob.value;
  const shared = knobNodes(knob).length > 1 ? `<span class="wf-shared">Shared by ${knobNodes(knob).map((id) => esc(ui.byId.get(id)?.title || id)).join(", ")}</span>` : "";
  const flags = `${knob.changed ? `<span class="wf-changed">Changed</span>` : ""}${edited ? `<span class="wf-edited">Not saved</span>` : ""}`;
  const disabled = locked ? "disabled" : "";
  let control;
  if (knob.type === "bool") {
    control = `<button type="button" class="wf-toggle${value ? " is-on" : ""}" role="switch" aria-checked="${value ? "true" : "false"}" data-act="toggle" data-key="${esc(knob.key)}" ${disabled} aria-label="${esc(knob.label)}">
      <span class="wf-toggle-track"><span class="wf-toggle-thumb"></span></span><span class="wf-toggle-text">${value ? "On" : "Off"}</span></button>`;
  } else {
    const min = Number(knob.min ?? 0);
    const max = Number(knob.max ?? 100);
    const step = Number(knob.step || (knob.type === "int" ? 1 : 0.01));
    const share = max > min ? clamp((Number(value) - min) / (max - min), 0, 1) : 0;
    const mark = max > min ? clamp((Number(knob.default) - min) / (max - min), 0, 1) : 0;
    control = `<div class="wf-slider-row">
      <div class="wf-slider" style="--pf:${share.toFixed(4)};--m:${mark.toFixed(4)}">
        <input type="range" class="wf-no-drag" data-knob="${esc(knob.key)}" min="${min}" max="${max}" step="${step}" value="${esc(value)}" ${disabled} aria-label="${esc(knob.label)}"/>
        <span class="wf-default-mark" title="Code value: ${esc(shownValue(knob, knob.default))}"><i></i></span>
      </div>
      <input type="number" class="wf-num" data-knob="${esc(knob.key)}" min="${min}" max="${max}" step="${step}" value="${esc(value)}" ${disabled} aria-label="${esc(knob.label)} value"/>
      <span class="wf-unit">${esc(knob.unit || "")}</span>
    </div>
    <div class="wf-range-ends"><span>${esc(plainNumber(min, 2))}</span><span>${esc(plainNumber(max, 2))}</span></div>`;
  }
  return `<div class="wf-knob${knob.changed ? " is-changed" : ""}${edited ? " is-edited" : ""}" data-knob-row="${esc(knob.key)}">
    <div class="wf-knob-head"><span class="wf-knob-label">${esc(knob.label)}</span>${flags}</div>
    ${control}
    <p class="wf-knob-help">${esc(knob.help || "")}</p>
    <p class="wf-knob-meta">Code value: <b>${esc(shownValue(knob, knob.default))}</b>${knob.changed ? ` · Saved: <b>${esc(shownValue(knob, knob.value))}</b>` : ""}${shared ? ` · ${shared}` : ""}</p>
  </div>`;
}

function syncKnob(key, source) {
  const knob = knobRows().find((row) => row.key === key);
  const drawer = ui.els.drawer;
  const value = knobValue(key);
  drawer.querySelectorAll(`[data-knob="${key}"]`).forEach((field) => {
    if (field !== source) field.value = String(value);
  });
  const row = drawer.querySelector(`[data-knob-row="${key}"]`);
  if (row && knob && knob.type !== "bool") {
    const min = Number(knob.min ?? 0);
    const max = Number(knob.max ?? 100);
    const share = max > min ? clamp((Number(value) - min) / (max - min), 0, 1) : 0;
    row.querySelector(".wf-slider")?.style.setProperty("--pf", share.toFixed(4));
    const edited = ui.draft[key] !== knob.value;
    row.classList.toggle("is-edited", edited);
    const head = row.querySelector(".wf-knob-head");
    let flag = head.querySelector(".wf-edited");
    if (edited && !flag) {
      flag = document.createElement("span");
      flag.className = "wf-edited";
      flag.textContent = "Not saved";
      head.appendChild(flag);
    } else if (!edited && flag) flag.remove();
  }
  updateKnobButtons();
}

function updateKnobButtons() {
  const node = ui.selected ? ui.byId.get(ui.selected) : null;
  if (!node) return;
  const keys = knobsFor(node).map((knob) => knob.key);
  const dirty = keys.some((key) => key in ui.draft && ui.draft[key] !== knobRows().find((row) => row.key === key)?.value);
  ui.els.drawer.querySelectorAll('[data-act="knob-save"], [data-act="knob-undo"]').forEach((button) => { button.disabled = !dirty; });
}

function toggleKnob(key) {
  if (!editable()) return;
  const knob = knobRows().find((row) => row.key === key);
  if (!knob) return;
  ui.draft[key] = !knobValue(key);
  if (ui.draft[key] === knob.value) delete ui.draft[key];
  renderDrawerPart("knobs");
}

function undoKnobs(nodeId) {
  const node = ui.byId.get(nodeId);
  if (!node) return;
  for (const knob of knobsFor(node)) delete ui.draft[knob.key];
  ui.knobNote = null;
  renderDrawerPart("knobs");
}

async function saveKnobs(nodeId) {
  const node = ui.byId.get(nodeId);
  if (!node || !editable()) return;
  const changes = {};
  for (const knob of knobsFor(node)) {
    if (knob.key in ui.draft && ui.draft[knob.key] !== knob.value) changes[knob.key] = ui.draft[knob.key];
  }
  if (!Object.keys(changes).length) return;
  ui.knobNote = { node: nodeId, tone: "busy", text: "Saving…" };
  renderDrawerPart("knobs");
  const result = await post(`${API}/knobs`, { knobs: changes });
  if (!result.ok) {
    ui.knobNote = { node: nodeId, tone: "bad", text: result.detail || "The desk did not save the settings." };
    renderDrawerPart("knobs");
    toast(ui.knobNote.text, "bad");
    return;
  }
  for (const key of Object.keys(changes)) delete ui.draft[key];
  takeDesign(result.data);
  const stamp = new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  ui.knobNote = { node: nodeId, tone: "good", text: `Saved at ${stamp}. Research reads the new values from its next step, and again whenever it starts.` };
  renderDrawerPart("knobs");
  toast("Settings saved to the desk.", "good");
  refreshCards();
}

async function resetKnobs(nodeId) {
  const node = ui.byId.get(nodeId);
  if (!node || !editable()) return;
  const keys = knobsFor(node).map((knob) => knob.key);
  ui.knobNote = { node: nodeId, tone: "busy", text: "Resetting…" };
  renderDrawerPart("knobs");
  const result = await post(`${API}/reset`, { keys });
  if (!result.ok) {
    ui.knobNote = { node: nodeId, tone: "bad", text: result.detail || "The desk did not reset the settings." };
    renderDrawerPart("knobs");
    return;
  }
  for (const key of keys) delete ui.draft[key];
  takeDesign(result.data);
  ui.knobNote = { node: nodeId, tone: "good", text: "Back to the code values." };
  renderDrawerPart("knobs");
  toast("Settings reset to the code values.", "good");
  refreshCards();
}

function queueList() {
  return ui.queueDraft || (Array.isArray(ui.design?.queue_first) ? ui.design.queue_first : []);
}

function queueHtml() {
  const locked = !editable();
  if (locked) {
    const slate = Array.isArray(ui.live.lab?.slate) ? ui.live.lab.slate : [];
    const rows = slate.map((item, index) => `<li><span class="wf-q-n">${index + 1}</span><span class="wf-ellip">${esc(item.file || item.title)}</span></li>`).join("");
    return `<section class="wf-d-queue is-locked"><h4>${uiIcon("lock")}Queue <small>read-only</small></h4>
      <div class="wf-lockline">${uiIcon("lock")}<span><b>Restart the desk to edit the queue.</b> Then you can choose which scripts are studied first.</span></div>
      ${rows ? `<p class="wf-d-src">Next on the research board:</p><ol class="wf-queue">${rows}</ol>` : ""}</section>`;
  }
  const first = queueList();
  const files = Array.isArray(ui.design?.learning_files) ? ui.design.learning_files : [];
  const dirty = ui.queueDraft && JSON.stringify(ui.queueDraft) !== JSON.stringify(ui.design?.queue_first || []);
  const firstRows = first.map((name, index) => `<li class="${files.includes(name) ? "" : "is-gone"}">
      <span class="wf-q-n">${index + 1}</span><span class="wf-ellip" title="${esc(name)}">${esc(name)}</span>
      <span class="wf-q-actions">
        <button type="button" class="wf-icon-btn" data-act="queue-up" data-name="${esc(name)}" ${index ? "" : "disabled"} aria-label="Move up">${uiIcon("up")}</button>
        <button type="button" class="wf-icon-btn" data-act="queue-down" data-name="${esc(name)}" ${index < first.length - 1 ? "" : "disabled"} aria-label="Move down">${uiIcon("down")}</button>
        <button type="button" class="wf-icon-btn" data-act="queue-remove" data-name="${esc(name)}" aria-label="Take off the list">${uiIcon("close")}</button>
      </span></li>`).join("");
  const rest = files.filter((name) => !first.includes(name)).map((name) => `<li>
      <span class="wf-ellip" title="${esc(name)}">${esc(name)}</span>
      <button type="button" class="wf-btn wf-btn-mini" data-act="queue-add" data-name="${esc(name)}">${uiIcon("add")}First</button></li>`).join("");
  const note = ui.queueNote ? `<p class="wf-save-note is-${ui.queueNote.tone}">${esc(ui.queueNote.text)}</p>` : "";
  return `<section class="wf-d-queue"><h4>${uiIcon("sliders")}Studied first <small>queue_first.json</small></h4>
    ${firstRows ? `<ol class="wf-queue">${firstRows}</ol>` : `<p class="wf-d-src">No script is named. The queue uses its own order.</p>`}
    <div class="wf-knob-actions">
      <button type="button" class="wf-btn wf-btn-primary" data-act="queue-save" ${dirty ? "" : "disabled"}>${uiIcon("save")}Save queue</button>
      <button type="button" class="wf-btn" data-act="queue-revert" ${dirty ? "" : "disabled"}>Undo edits</button>
    </div>${note}
    <details class="wf-d-sec" open><summary><span>Learning folder, in the order the engines take it</span><b>${files.length}</b></summary>
      <ul class="wf-queue wf-queue-rest">${rest || `<li><span class="wf-d-src">Every learning script is on the list above.</span></li>`}</ul></details>
  </section>`;
}

const QUESTION_KINDS = {
  compile: "Does not compile",
  repair: "Compile repair running",
  entry_rule: "No entry rule",
  unrunnable: "Stops in PineTS",
  no_trades: "Never trades",
  over_trades: "Too many trades",
};

function questionRows() {
  const rows = ui.design?.live?.questions;
  return Array.isArray(rows) ? rows : [];
}

function questionsHtml() {
  const rows = questionRows();
  const note = ui.questionNote ? `<p class="wf-save-note is-${ui.questionNote.tone}">${esc(ui.questionNote.text)}</p>` : "";
  if (!rows.length) {
    return `<section class="wf-d-questions"><h4>${uiIcon("file")}Open questions</h4>
      <p class="wf-d-src">No question is waiting for an answer.</p>${note}</section>`;
  }
  const locked = !editable();
  const items = rows.map((row) => {
    const chart = row.chart ? [row.chart.root, row.chart.timeframe].filter(Boolean).join(" ") : "";
    const options = (Array.isArray(row.options) ? row.options : []).map((item) => `<li>${esc(item.n)}. ${esc(item.text)}</li>`).join("");
    return `<li class="wf-question">
      <p class="wf-q-head"><b>${esc(row.qid)}</b><span class="wf-chip">${esc(QUESTION_KINDS[row.kind] || row.kind)}</span>${chart ? `<span class="wf-chip">${esc(chart)}</span>` : ""}</p>
      <p class="wf-ellip" title="${esc(row.file)}">${esc(row.file)}</p>
      <p class="wf-d-src">Asked ${esc(row.asked_at || "")} by the ${esc(row.asked_by || "Chart researcher")}${row.sent_at ? ` · emailed ${esc(row.sent_at)}` : " · not emailed yet"}</p>
      <p class="wf-d-src">Meanwhile: ${esc(row.applied_default || "")}</p>
      ${options ? `<ol class="wf-q-options">${options}</ol>` : ""}
      <textarea class="wf-q-answer" rows="3" data-qid="${esc(row.qid)}" placeholder="A number, lines like stop_dollars = 300, park, retry, or a fixed script" ${locked ? "disabled" : ""}></textarea>
      <div class="wf-knob-actions"><button type="button" class="wf-btn wf-btn-primary" data-act="answer-question" data-qid="${esc(row.qid)}" ${locked ? "disabled" : ""}>${uiIcon("save")}Send answer</button></div>
    </li>`;
  }).join("");
  return `<section class="wf-d-questions"><h4>${uiIcon("file")}Open questions <small>${rows.length}</small></h4>
    ${locked ? `<div class="wf-lockline">${uiIcon("lock")}<span><b>Restart the desk to answer here.</b> Email answers still work.</span></div>` : ""}
    <ul class="wf-questions">${items}</ul>${note}</section>`;
}

async function answerQuestion(qid) {
  if (!editable() || !qid) return;
  const box = ui.els.drawer.querySelector(`textarea[data-qid="${CSS.escape(qid)}"]`);
  const text = box ? box.value.trim() : "";
  if (!text) {
    ui.questionNote = { tone: "bad", text: "Write an answer first." };
    renderDrawerPart("questions", true);
    return;
  }
  ui.questionNote = { tone: "busy", text: "Sending…" };
  const result = await post(`/api/questions/${encodeURIComponent(qid)}/answer`, { text });
  if (!result.ok) {
    ui.questionNote = { tone: "bad", text: result.detail || "The desk did not take the answer." };
    renderDrawerPart("questions", true);
    return;
  }
  if (box) box.value = "";
  ui.questionNote = { tone: "good", text: result.data?.reason || `${qid} was passed to the researcher who asked.` };
  renderDrawerPart("questions", true);
  toast(`${qid} answered.`, "good");
  setTimeout(() => pollDesign(), 1500);
}

function editQueue(act, name) {
  if (!editable() || !name) return;
  const list = [...queueList()];
  const at = list.indexOf(name);
  if (act === "queue-add" && at < 0) list.push(name);
  else if (act === "queue-remove" && at >= 0) list.splice(at, 1);
  else if (act === "queue-up" && at > 0) [list[at - 1], list[at]] = [list[at], list[at - 1]];
  else if (act === "queue-down" && at >= 0 && at < list.length - 1) [list[at + 1], list[at]] = [list[at], list[at + 1]];
  ui.queueDraft = list;
  ui.queueNote = null;
  renderDrawerPart("queue");
}

async function saveQueue() {
  if (!editable() || !ui.queueDraft) return;
  ui.queueNote = { tone: "busy", text: "Saving…" };
  renderDrawerPart("queue");
  const result = await post(`${API}/queue`, { names: ui.queueDraft });
  if (!result.ok) {
    ui.queueNote = { tone: "bad", text: result.detail || "The desk did not save the queue." };
    renderDrawerPart("queue");
    return;
  }
  if (ui.design) ui.design.queue_first = result.data?.queue_first || ui.queueDraft;
  ui.queueDraft = null;
  ui.queueNote = { tone: "good", text: "Saved. The engines read the new order at their next scan." };
  renderDrawerPart("queue");
  toast("Queue saved.", "good");
  refreshCards();
  setTimeout(() => pollDesign(), 1500);
}

function agentsHtml(node) {
  const names = Array.isArray(node.agents) ? node.agents : [];
  if (!names.length) return "";
  const rows = names.map((name) => {
    const meta = agentMeta(name);
    const status = statusOf(name);
    const line = latestLine(name);
    return `<li class="wf-agent is-${status}" style="--ac:${esc(meta.color)}">
      ${avatar(name, "md")}
      <div class="wf-agent-text">
        <p class="wf-agent-name"><b>${esc(name)}</b><span class="wf-status is-${status}">${esc(STATUS_TEXT[status] || status)}</span></p>
        <p class="wf-agent-role">${esc(meta.role || "")}</p>
        ${line ? `<p class="wf-agent-line" title="${esc(line)}">${esc(line)}</p>` : ""}
      </div></li>`;
  }).join("");
  return `<details class="wf-d-sec" open><summary><span>Agents</span><b>${names.length}</b></summary><ul class="wf-agents">${rows}</ul></details>`;
}

function spotName(id) {
  const spot = ui.spots[id];
  if (!spot) return "";
  return ui.byId.get(spot.node)?.title || "";
}

// "attempt 12 of 100 · 7-day test": the attempt, the budget of the search it is in, and its test.
function engineAttempt(engine) {
  const attempt = Number(engine?.attempt) || 0;
  const limit = Number(engine?.attempt_limit) || 0;
  const stage = Number(engine?.stage) || 0;
  const test = stage === 365 ? "year" : stage ? `${stage}-day test` : "";
  if (!attempt && !test) return "";
  if (!attempt) return `${test}${limit ? ` · ${limit} attempts` : ""}`;
  return `attempt ${attempt}${limit ? ` of ${limit}` : ""}${test ? ` · ${test}` : ""}`;
}

function enginesHtml(node) {
  const ids = (node.engines || []).map(engineIdOf).filter(Boolean);
  if (!ids.length) return "";
  const rows = ids.map((id) => {
    const engine = engineById(id);
    const chart = engineChart(id);
    if (!engine) {
      return `<li class="wf-engine"><span class="wf-chip">E${id}</span><div><p><b>Engine ${id}</b> <small>${esc(chart)}</small></p><p class="wf-d-src">No live data yet.</p></div></li>`;
    }
    const state = engine.busy ? "busy" : engine.studying ? "open" : "idle";
    const move = engine.scale_move ? `<span class="wf-move is-${esc(engine.scale_move)}">${esc(engine.scale_move === "closer" ? "closer to the goal" : engine.scale_move === "farther" ? "farther from the goal" : engine.scale_move)}</span>` : "";
    const where = spotName(id);
    const detail = [
      engine.instrument ? `${engine.instrument} ${engine.timeframe || ""}`.trim() : engine.timeframe || chart,
      engineAttempt(engine),
      engine.scale_phase || "",
    ].filter(Boolean).join(" · ");
    return `<li class="wf-engine is-${state}">
      <span class="wf-chip ${engine.busy ? "is-busy" : engine.studying ? "is-open" : ""}">E${id}</span>
      <div class="wf-engine-text">
        <p><b>Engine ${id}</b> <small>${esc(chart)} · ${esc(engine.developer || "")}</small><span class="wf-status is-${engine.busy ? "working" : engine.studying ? "pending" : "idle"}">${engine.busy ? "Running a backtest" : engine.studying ? "Script open" : "Idle"}</span></p>
        ${engine.study ? `<p class="wf-ellip" title="${esc(engine.study)}">${esc(engine.study)}</p>` : ""}
        <p class="wf-engine-meta">${esc(detail)}${where ? ` · at <button type="button" class="wf-inline-link" data-act="goto" data-node="${esc(ui.spots[id].node)}">${esc(where)}</button>` : ""} ${move}</p>
        ${engine.notice ? `<p class="wf-engine-notice">${esc(engine.notice)}</p>` : ""}
        ${engine.thinking ? `<p class="wf-engine-notice is-think">${esc(engine.thinking)}</p>` : ""}
      </div></li>`;
  }).join("");
  // 22 engines: the list starts folded on a step every engine passes through.
  return `<details class="wf-d-sec"${ids.length <= 8 ? " open" : ""}><summary><span>Engines</span><b>${ids.length}</b></summary><ul class="wf-engines">${rows}</ul></details>`;
}

// ---------- live data ----------

function testingRows() {
  if (Array.isArray(ui.design?.live?.testing)) return ui.design.live.testing;
  const rows = Array.isArray(ui.proving?.rows) ? ui.proving.rows : [];
  return rows.filter((row) => row.status === "testing").map((row) => ({ file: row.file, root: row.root, timeframe: row.timeframe, stage: row.stage }));
}

function ledgerNow() {
  if (ui.design?.live?.ledger) return ui.design.live.ledger;
  const rows = Array.isArray(ui.proving?.rows) ? ui.proving.rows : null;
  if (!rows) return null;
  const ledger = {};
  for (const days of [7, 30, 90, 365]) ledger[String(days)] = { testing: 0, passed: 0, failed: 0, stopped: 0 };
  for (const row of rows) {
    const stage = String(Number(row.stage) || 0);
    const passed = row.passed && typeof row.passed === "object" ? row.passed : {};
    for (const days of Object.keys(passed)) if (ledger[days]) ledger[days].passed += 1;
    const bucket = ledger[stage];
    if (!bucket) continue;
    if (row.status === "testing" || row.status === "failed" || row.status === "stopped") bucket[row.status] += 1;
    else if (row.status === "passed" && !(stage in passed)) bucket.passed += 1;
  }
  return ledger;
}

function isLearningScript(file) {
  if (!file) return false;
  if (Array.isArray(ui.design?.learning_files) && ui.design.learning_files.includes(file)) return true;
  if (engineRows().some((engine) => !isCreation(engine) && engine.study === file)) return true;
  return (ui.live.lab?.slate || []).some((item) => item.file === file);
}

function computeSpots() {
  const rows = testingRows();
  const spots = {};
  const loads = {};
  for (const engine of engineRows()) {
    const id = Number(engine.id);
    if (!engine.studying && !engine.busy) continue;
    const file = engine.study || "";
    const matches = rows.filter((row) => row.file === file && (!engine.timeframe || row.timeframe === engine.timeframe));
    const row = matches.find((item) => engine.instrument && item.root === engine.instrument) || matches[0];
    let node;
    if (row) node = DAYS_NODE[Number(row.stage)] || "year";
    else if (isCreation(engine) && !isLearningScript(file)) node = "engine4";
    // The engine says which test its search is on, so a stage shows before the ledger file is read again.
    else if (Number(engine.stage)) node = DAYS_NODE[Number(engine.stage)] || "year";
    else if (engine.notice) node = "bars";
    else node = "read";
    spots[id] = { node, row: row || null };
    loads[node] = (loads[node] || 0) + 1;
  }
  ui.spots = spots;
  ui.loads = loads;
}

function learningFallback() {
  const rows = ui.live.lab?.progress?.rows;
  if (!Array.isArray(rows) || !rows.length) return null;
  return Math.max(...rows.map((row) => Number(row.learning_total) || 0));
}

function liveFor(node) {
  const live = ui.design?.live;
  const folders = live?.folders || {};
  const key = node.live;
  if (!key) return null;
  if (key === "learning") {
    const count = folders.learning ?? learningFallback();
    return count == null ? null : { value: count, label: "scripts in learningStrategies" };
  }
  if (key === "queue") {
    const list = ui.design?.queue_first;
    return Array.isArray(list) ? { value: list.length, label: "scripts named first" } : null;
  }
  if (key === "claim") {
    const rows = engineRows().filter((engine) => !isCreation(engine));
    if (!rows.length) return null;
    const busy = rows.filter((engine) => engine.busy).length;
    const waiting = rows.filter((engine) => /^Waiting for a free worker/.test(String(engine.notice || ""))).length;
    return { value: `${busy}/${rows.length}`, label: `chart engines backtesting now${waiting ? ` · ${waiting} waiting for a worker` : ""}`, tone: busy ? "hot" : "" };
  }
  if (key === "bars") {
    const count = engineRows().filter((engine) => engine.notice && !/^Waiting for a free worker/.test(String(engine.notice))).length;
    return count ? { value: count, label: "engines waiting on bars", tone: "warn" } : null;
  }
  if (key === "cannot_run") return folders.cannot_run != null ? { value: folders.cannot_run, label: "scripts parked", tone: folders.cannot_run ? "bad" : "" } : null;
  if (key === "awaiting") {
    const open = Array.isArray(live?.questions) ? live.questions.length : null;
    if (folders.awaiting == null && open == null) return null;
    const count = folders.awaiting ?? 0;
    return { value: count, label: open ? `scripts waiting · ${open} open question${open === 1 ? "" : "s"}` : "scripts waiting for an answer", tone: count ? "warn" : "" };
  }
  if (NODE_DAYS[key]) {
    const ledger = ledgerNow();
    const row = ledger?.[String(NODE_DAYS[key])];
    return row ? { value: row.testing || 0, label: "testing now", tone: row.testing ? "hot" : "" } : null;
  }
  if (key === "waiting") return live && live.waiting != null ? { value: live.waiting, label: "runs on the Not-tested list" } : null;
  if (key === "shelf") return live && live.shelf != null ? { value: live.shelf, label: "rows on the five-year shelf" } : null;
  if (["learned", "drafts", "created", "unproven"].includes(key)) {
    return folders[key] != null ? { value: folders[key], label: "scripts in the folder" } : null;
  }
  if (key === "engine4") {
    const engine = creationEngine();
    if (!engine) return null;
    const where = ui.spots[engine.id]?.node;
    if (engine.busy && where === "engine4") return { value: "Draft", label: "developing a draft", tone: "hot" };
    if (engine.studying && where && where !== "engine4") return { value: "Learning", label: "studying a learning script", tone: "warn" };
    return { value: engine.busy ? "Busy" : "Idle", label: `engine ${engine.id}`, tone: engine.busy ? "hot" : "" };
  }
  return null;
}

function goalName(goal) {
  return { profit_factor: "Profit factor", net_profit: "Profit", profit: "Profit", win_rate: "Win rate", drawdown: "Drawdown", trades: "Trades" }[goal] || String(goal || "Profit").replace(/_/g, " ");
}

function setText(el, text) {
  if (el && el.textContent !== String(text)) el.textContent = String(text);
}

function setHtml(el, html) {
  if (!el || el.dataset.sig === html) return;
  el.dataset.sig = html;
  el.innerHTML = html;
}

function refreshCards() {
  if (!ui.cards) return;
  const ledger = ledgerNow();
  for (const node of ui.nodes) {
    const card = ui.cards.get(node.id);
    if (!card) continue;
    const kind = kindOf(node);
    const live = liveFor(node);
    const counter = card.querySelector('[data-role="counter"]');
    const flag = card.querySelector('[data-role="live"]');
    const days = NODE_DAYS[node.id];
    if (days) {
      const row = ledger?.[String(days)];
      const judged = row ? (row.passed || 0) + (row.failed || 0) : 0;
      const rate = judged ? (row.passed || 0) / judged : 0;
      const ring = card.querySelector(".wf-ring");
      if (ring) {
        const offset = (RING * (1 - rate)).toFixed(2);
        if (ring.getAttribute("stroke-dashoffset") !== offset) ring.setAttribute("stroke-dashoffset", offset);
      }
      if (flag) flag.hidden = true;
      const stats = card.querySelector('[data-role="stats"]');
      if (stats) {
        setHtml(stats, row
          ? `<b class="wf-pass">${row.passed || 0}</b> passed <b class="wf-fail">${row.failed || 0}</b> failed <b class="wf-rate">${judged ? `${Math.round(rate * 100)}%` : "—"}</b>`
          : `<span class="wf-dim">Ledger not read yet</span>`);
        stats.title = row ? `${row.passed || 0} passed, ${row.failed || 0} failed, ${row.stopped || 0} stopped. Pass rate ${judged ? Math.round(rate * 100) : 0}%.` : "";
      }
      const testing = row?.testing || 0;
      counter.hidden = !testing;
      setText(counter, testing);
      counter.title = `${testing} testing now`;
      counter.className = `wf-counter${testing ? " is-hot" : ""}`;
    } else if (live && counter) {
      counter.hidden = false;
      setText(counter, live.value);
      counter.title = live.label;
      counter.className = `wf-counter${live.tone ? ` is-${live.tone}` : ""}`;
      if (flag) flag.hidden = true;
    } else if (counter) {
      counter.hidden = true;
      if (flag) flag.hidden = true;
    }
    if (node.id === "goal") {
      const badge = card.querySelector('[data-role="badge"]');
      const week = weeklyPace();
      if (badge && week) setText(badge, week.badge);
    }
    const chips = card.querySelector('[data-role="chips"]');
    let busyHere = false;
    if (chips) {
      let ids;
      if (kind === "engine") ids = (node.engines || []).map(engineIdOf).filter(Boolean);
      else ids = Object.entries(ui.spots).filter(([, spot]) => spot.node === node.id).map(([id]) => Number(id));
      // With 22 engines a card shows the busy and open ones first and folds the rest into "+N".
      const rank = (id) => {
        const engine = engineById(id);
        return engine?.busy ? 0 : engine?.studying ? 1 : 2;
      };
      const sorted = ids.sort((a, b) => (rank(a) - rank(b)) || (a - b));
      for (const id of sorted) {
        const engine = engineById(id);
        if (engine?.busy && (kind === "engine" ? true : ui.spots[id]?.node === node.id)) busyHere = true;
      }
      const shown = sorted.length > CARD_CHIPS ? sorted.slice(0, CARD_CHIPS - 1) : sorted;
      const rest = sorted.slice(shown.length);
      const more = rest.length ? `<span class="wf-chip" title="${esc(rest.map((id) => `E${id} ${engineChart(id)}`).join(", "))}">+${rest.length}</span>` : "";
      const html = sorted.length ? shown.map((id) => {
        const engine = engineById(id);
        const cls = engine?.busy ? "is-busy" : engine?.studying ? "is-open" : "";
        const tip = engine ? `Engine ${id} (${engineChart(id)})${engine.study ? `: ${engine.study}` : ""}${engine.instrument ? ` on ${engine.instrument} ${engine.timeframe || ""}` : ""}${engineAttempt(engine) ? `, ${engineAttempt(engine)}` : ""}` : `Engine ${id}`;
        return `<span class="wf-chip ${cls}" title="${esc(tip)}">E${id}</span>`;
      }).join("") + more : `<span class="wf-chip-empty">${kind === "stage" ? "No engine here now" : "No engine data"}</span>`;
      setHtml(chips, html);
      if (kind === "engine" && node.id === "engine4") {
        const engine = creationEngine();
        busyHere = Boolean(engine?.busy);
      }
    }
    card.classList.toggle("is-busy", busyHere || (ui.loads[node.id] || 0) > 0);
    const main = MAIN_SWITCH[node.id];
    const off = main ? savedValue(main) === false : false;
    card.classList.toggle("is-off", off);
    let ribbon = card.querySelector(".wf-off");
    if (off && !ribbon) {
      ribbon = document.createElement("span");
      ribbon.className = "wf-off";
      ribbon.textContent = "Off";
      card.appendChild(ribbon);
    } else if (!off && ribbon) ribbon.remove();
    if (ribbon) ribbon.title = `${knobRows().find((knob) => knob.key === main)?.label || "This step"} is off, so research skips this step.`;
    const knobFlag = card.querySelector('[data-role="knob-flag"]');
    if (knobFlag) knobFlag.classList.toggle("is-changed", knobsFor(node).some((knob) => knob.changed));
    card.querySelectorAll(".wf-ava[data-agent]").forEach((ava) => {
      const status = statusOf(ava.dataset.agent);
      const cls = `wf-ava wf-ava-sm is-${status}`;
      if (ava.className !== cls) {
        ava.className = cls;
        ava.title = `${ava.dataset.agent}: ${STATUS_TEXT[status] || status}`;
      }
    });
  }
}

function renderGoalTitle() {
  const title = ui.catalog?.goal?.title || "Low drawdown, profit, and the library trade count at a steady pace";
  setText(ui.els["goal-title"], title);
}

// What each test needs, from objective.stage_bars through /api/book (acceptance_stages: 7, 30, 90, 365 days).
function stageBars() {
  const stages = ui.live.book?.acceptance_stages;
  return Array.isArray(stages) && stages.length ? stages : null;
}

function stageList(stages, pick) {
  return stages.map((bar) => `${bar.days >= 365 ? "year" : `${bar.days} days`} ${pick(bar)}`).join(", ");
}

// The library numbers at a steady pace: a week, and each test. Null until /api/book carries the bars.
function weeklyPace() {
  const stages = stageBars();
  if (!stages) return null;
  const year = stages.find((bar) => Number(bar.days) >= 365) || stages[stages.length - 1];
  const parts = [];
  if (year.use_profit) parts.push(`${money(year.weekly_profit)} a week`);
  if (year.use_trades) parts.push(`${plainNumber(year.weekly_trades, 1)} trades a week`);
  const floor = Number(year.floor_week) || 0;
  if (floor > 0) parts.push(`at least ${plainNumber(floor, 1)} a week (extra floor)`);
  const label = parts.length ? parts.join(" · ") : "Profit above $0";
  const badge = year.use_trades ? `${plainNumber(Math.max(year.weekly_trades, floor), 1)}/wk` : floor > 0 ? `${plainNumber(floor, 1)}/wk` : "";
  const tip = [
    "The library boxes at a steady pace: each test needs its share of the year, sessions out of 260 (5, 21 and 64).",
    year.use_profit ? `Profit above: ${stageList(stages, (bar) => money(bar.profit))}.` : "Profit above $0 on every test.",
    `Trades, fewest to most: ${stageList(stages, (bar) => `${bar.min_trades} to ${bar.max_trades}`)}.`,
    floor > 0 ? `The extra floor of ${plainNumber(floor, 1)} trades a week is on.` : "The extra trades-a-week floor is off, so the trade count comes from the library box only.",
  ].join(" ");
  return { label, badge, tip, year, stages };
}

function renderHud() {
  const acc = ui.live.book?.strategy_acceptance;
  const week = weeklyPace();
  const chips = [];
  const chip = (on, label, tip, tone = "") => chips.push(`<span class="wf-obj${on ? " is-on" : " is-off"}${tone ? ` ${tone}` : ""}" title="${esc(tip)}"><i></i>${esc(label)}</span>`);
  if (week) chip(true, week.label, week.tip, "is-key");
  if (acc) {
    const stages = week?.stages;
    chip(Boolean(acc.use_drawdown), "Drawdown: pro rule", "The pro account drawdown rule: each dip may reach $2,500 until profit passes $3,000, then 30% of every dollar above it. The same limit applies on 7, 30, 90 and 365 days, because a short window's drawdown is never larger than the year that holds it.");
    chip(true, acc.use_profit ? `Profit ${money(acc.min_profit)}+ a year` : "Profit above $0", acc.use_profit ? `The profit box, pro-rated to each test: ${stages ? stageList(stages, (bar) => money(bar.profit)) : "sessions out of 260"}.` : "The profit box is off, so any profit above zero passes.");
    chip(Boolean(acc.use_factor), `Profit factor ${plainNumber(acc.profit_factor, 2)}+`, "The profit factor box. A ratio, so it is the same on every test.");
    chip(Boolean(acc.use_win_rate), `Win rate ${plainNumber(acc.win_rate, 1)}%+`, "The win rate box. A ratio, so it is the same on every test.");
    chip(Boolean(acc.use_trades), `${plainNumber(acc.min_trades, 0)}+ trades a year`, `The trade count box, pro-rated to each test: ${stages ? stageList(stages, (bar) => `at least ${bar.min_trades}`) : "sessions out of 260"}. At most 600 a year at the same pace.`);
    chip(true, "Stop $750 at most", "One trade may not risk more than $750, 1.5% of the $50,000 account. Always checked.");
    chips.push(`<span class="wf-obj is-steer" title="The goal box steers scale_move, which judges whether each change moved closer to the goal."><i></i>Steers by ${esc(goalName(acc.goal).toLowerCase())}</span>`);
  } else {
    chips.push(`<span class="wf-obj is-wait"><i></i>Reading the acceptance boxes…</span>`);
  }
  setHtml(ui.els.objectives, chips.join(""));
  const research = ui.live.snapshot?.research || "idle";
  const holding = Boolean(ui.live.snapshot?.hold_new);
  const label = { running: "Research running", stopping: "Research stopping", stopped: "Research stopped", idle: "Research idle" }[research] || `Research ${research}`;
  setHtml(ui.els.research, `<span class="wf-led is-${esc(research)}"></span><span class="wf-research-text">${esc(label)}</span>${holding ? `<span class="wf-hold" title="The strategy developers are paused">Hold on</span>` : ""}`);
  const engines = engineRows();
  const busy = engines.filter((engine) => engine.busy).length;
  const leds = engines.length ? engines.slice().sort((a, b) => Number(a.id) - Number(b.id)).map((engine) => {
    const cls = engine.busy ? "is-busy" : engine.studying ? "is-open" : "";
    const tip = `Engine ${engine.id} (${engineChart(engine.id)}): ${engine.busy ? "running a backtest" : engine.studying ? "script open" : "idle"}${engine.study ? `, ${engine.study}` : ""}`;
    return `<span class="wf-eled ${cls}" title="${esc(tip)}">${esc(engine.id)}</span>`;
  }).join("") : "";
  setHtml(ui.els["engine-leds"], `<span class="wf-eled-label"><b>${busy}</b>/${engines.length || ENGINE_COUNT} busy</span><span class="wf-eleds">${leds}</span>`);
  const summary = ui.changes?.summary;
  const xp = ui.els.xp;
  if (summary && summary.total) {
    const judged = (summary.toward || 0) + (summary.away || 0) + (summary.flat || 0);
    const share = judged ? (summary.toward || 0) / judged : 0;
    xp.querySelector(".wf-xp-fill").style.width = `${(share * 100).toFixed(1)}%`;
    setText(xp.querySelector(".wf-xp-value"), `${summary.toward || 0} of ${judged}`);
    xp.title = `${summary.toward || 0} changes moved toward the goal, ${summary.away || 0} away and ${summary.flat || 0} made no clear change in the last ${ui.changes.hours || 72} hours. ${summary.passes || 0} rows passed acceptance.`;
    xp.classList.remove("is-empty");
  } else {
    xp.querySelector(".wf-xp-fill").style.width = "0%";
    setText(xp.querySelector(".wf-xp-value"), ui.changesApi === "missing" ? "after restart" : "—");
    xp.classList.add("is-empty");
  }
  ui.els.banner.hidden = ui.api !== "missing";
}

function updateLive() {
  if (!ui.built) return;
  computeSpots();
  renderHud();
  refreshCards();
  renderParticles();
  placeMinimap();
  if (ui.selected) {
    for (const part of ["live", "agents", "engines", "queue", "questions"]) renderDrawerPart(part);
  }
}

function scheduleLive() {
  if (ui.liveQueued) return;
  ui.liveQueued = true;
  requestAnimationFrame(() => {
    ui.liveQueued = false;
    if (ui.visible) updateLive();
  });
}

function takeDesign(data) {
  if (!data || typeof data !== "object") return;
  ui.design = { ...(ui.design || {}), ...data };
  ui.api = "ok";
  const server = data.layout && typeof data.layout === "object" ? data.layout : null;
  if (server && !ui.layoutDirty && !ui.drag) {
    if (Object.keys(server).length) {
      const before = JSON.stringify(ui.pos);
      ui.pos = Object.fromEntries(Object.entries(ui.base).map(([id, place]) => [id, [...place]]));
      applyLayout(server);
      ui.layoutSource = "desk";
      if (JSON.stringify(ui.pos) !== before) relayout();
    } else if (ui.layoutSource === "browser") {
      ui.layoutDirty = true;
    }
    updateLayoutButtons();
  }
  if (ui.selected) renderDrawerPart("knobs");
}

// ---------- network ----------

async function getJson(url) {
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (response.status === 404 || response.status === 405) return { missing: true };
    if (!response.ok) return { error: true };
    return { data: await response.json() };
  } catch (error) {
    return { error: true };
  }
}

async function post(url, body) {
  try {
    const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 404) {
      ui.api = "missing";
      renderHud();
      return { ok: false, detail: "The desk does not have this page's settings yet. Restart the desk to edit settings." };
    }
    if (!response.ok) {
      const detail = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg || "").join(" ") : payload.detail;
      return { ok: false, detail: detail || `The desk answered ${response.status}.` };
    }
    return { ok: true, data: payload };
  } catch (error) {
    return { ok: false, detail: "The desk did not answer." };
  }
}

async function pollLive() {
  if (Date.now() - ui.live.fedAt < FEED_FRESH_MS) return;
  const [snapshot, lab, book] = await Promise.all([getJson("/api/state"), getJson("/api/lab"), getJson("/api/book")]);
  if (Date.now() - ui.live.fedAt < FEED_FRESH_MS) return;
  if (snapshot.data) ui.live.snapshot = snapshot.data;
  if (lab.data) ui.live.lab = lab.data;
  if (book.data) ui.live.book = book.data;
  updateLive();
}

async function pollDesign() {
  const result = await getJson(API);
  if (result.data) takeDesign(result.data);
  else if (result.missing) ui.api = "missing";
  else if (ui.api === "unknown") ui.api = "error";
  if (ui.api !== "ok") {
    const proving = await getJson("/api/proving");
    if (proving.data) ui.proving = proving.data;
  }
  updateLive();
}

async function pollChanges() {
  const result = await getJson(`${API}/changes?limit=80`);
  if (result.data) {
    ui.changes = result.data;
    ui.changesApi = "ok";
  } else if (result.missing) ui.changesApi = "missing";
  else if (ui.changesApi === "unknown") ui.changesApi = "error";
  renderImpact(false);
  renderHud();
}

// While the desk runs code without the design routes, ask less often; the ledger changes slowly.
const TASKS = [
  { name: "live", every: () => 3000, run: pollLive },
  { name: "design", every: () => (ui.api === "missing" ? 30000 : 10000), run: pollDesign },
  { name: "changes", every: () => (ui.changesApi === "missing" ? 60000 : 30000), run: pollChanges },
];

function startPolling() {
  if (ui.polling || document.hidden) return;
  const generation = Date.now();
  ui.polling = generation;
  for (const task of TASKS) {
    const loop = async () => {
      if (ui.polling !== generation) return;
      try {
        await task.run();
      } catch (error) {
        // A failed poll keeps the last data; the next poll tries again.
      }
      if (ui.polling !== generation) return;
      ui.timers[task.name] = setTimeout(loop, task.every());
    };
    loop();
  }
}

function stopPolling() {
  ui.polling = 0;
  for (const timer of Object.values(ui.timers)) clearTimeout(timer);
  ui.timers = {};
}

// ---------- change impact ----------

function renderImpactShell() {
  renderImpact(true);
}

function verdictOf(row) {
  return VERDICTS[row.verdict] ? row.verdict : "flat";
}

function impactRowsShown() {
  const rows = Array.isArray(ui.changes?.rows) ? ui.changes.rows : [];
  return ui.filter === "all" ? rows : rows.filter((row) => verdictOf(row) === ui.filter);
}

function renderImpact(force) {
  if (!ui.els.impact) return;
  const summary = ui.changes?.summary || { total: 0, toward: 0, away: 0, flat: 0, no_trades: 0, passes: 0 };
  const total = summary.total || 0;
  const segments = ["toward", "away", "flat", "no_trades"].map((key) => {
    const count = summary[key] || 0;
    return count ? `<span class="wf-seg" style="flex:${count};--vc:${VERDICTS[key].color}" title="${esc(VERDICTS[key].label)}: ${count}"></span>` : "";
  }).join("");
  let mini;
  if (ui.changesApi === "missing") mini = `<span class="wf-impact-wait">${uiIcon("lock")}Shows each change and what it did to profit, drawdown and trades after the desk restarts.</span>`;
  else if (!ui.changes) mini = `<span class="wf-impact-wait">Reading the research notes…</span>`;
  else if (!total) mini = `<span class="wf-impact-wait">No attempts in the research notes in the last ${ui.changes.hours || 72} hours.</span>`;
  else {
    mini = `<span class="wf-segbar">${segments}</span>
      ${["toward", "away", "flat", "no_trades"].map((key) => `<span class="wf-vcount" style="--vc:${VERDICTS[key].color}"><i></i>${summary[key] || 0} ${esc(VERDICTS[key].short.toLowerCase())}</span>`).join("")}
      <span class="wf-vcount is-pass"><i></i>${summary.passes || 0} passed</span>`;
  }
  setHtml(ui.els["impact-mini"], mini);
  const missing = ui.changesApi === "missing";
  ui.els.impact.classList.toggle("is-missing", missing);
  ui.section.classList.toggle("wf-impact-missing", missing);
  if (!ui.impactOpen && !force) return;
  const filters = [["all", "All", total], ...Object.entries(VERDICTS).map(([key, meta]) => [key, meta.short, summary[key] || 0])]
    .map(([key, label, count]) => `<button type="button" class="wf-filter${ui.filter === key ? " is-on" : ""}" data-act="filter" data-filter="${key}" style="--vc:${key === "all" ? "#7dd3fc" : VERDICTS[key].color}" aria-pressed="${ui.filter === key ? "true" : "false"}">${esc(label)}<b>${count}</b></button>`).join("");
  setHtml(ui.els.filters, filters);
  const bars = ["toward", "away", "flat", "no_trades"].map((key) => {
    const count = summary[key] || 0;
    const share = total ? (count / total) * 100 : 0;
    return `<div class="wf-sumbar" style="--vc:${VERDICTS[key].color}"><span>${esc(VERDICTS[key].short)}</span><span class="wf-sumbar-track"><span style="width:${share.toFixed(1)}%"></span></span><b>${count}</b></div>`;
  }).join("");
  setHtml(ui.els.sumbars, bars);
  let note = "Each row is one attempt, compared with the attempt before it on the same chart and stage. The verdict is the desk's own judge, scale_move. Click a row to open its stage.";
  if (ui.changesApi === "missing") note = "The change list needs the new desk code. Restart the desk to see what each change did against the goal.";
  else if (ui.changes?.built) note += ` Built ${ui.changes.built} from ${ui.changes.files ?? "?"} notes files.`;
  setText(ui.els["impact-note"], note);
  const rows = impactRowsShown();
  const body = rows.length ? rows.map((row) => {
    const verdict = verdictOf(row);
    const meta = VERDICTS[verdict];
    const dp = Number(row.d_profit);
    const dd = Number(row.d_drawdown);
    const dt = Number(row.d_trades);
    const misses = (row.misses || []).map((miss) => `<span class="wf-miss">${esc(MISS_TEXT[miss] || String(miss).replace(/_/g, " "))}</span>`).join("") || (row.passes ? `<span class="wf-miss is-pass">Passes</span>` : "");
    const stage = Number(row.stage) === 365 ? "1Y" : `${row.stage}D`;
    const tone = (value, goodUp) => (!Number.isFinite(value) || value === 0 ? "" : (value > 0) === goodUp ? "is-good" : "is-bad");
    return `<tr class="wf-row is-${verdict}" data-act="change-row" data-stage="${esc(row.stage)}" style="--vc:${meta.color}" tabindex="0">
      <td class="wf-when" title="${row.noted ? "" : "The proving ledger no longer lists this chart; this is when its notes file was last written."}">${row.noted ? esc(String(row.noted).slice(5)) : row.noted_before ? `before ${esc(String(row.noted_before).slice(5))}` : ""}</td>
      <td class="wf-script" title="${esc(row.file)}">${esc(row.file)}</td>
      <td><span class="wf-chip">${esc(row.root || "")} ${esc(row.timeframe || "")}</span></td>
      <td><span class="wf-stage-tag">${esc(stage)}</span><small>#${esc(row.attempt ?? "")}</small></td>
      <td class="wf-change" title="${esc(row.impact || row.change)}">${esc(row.change || "Starting settings")}</td>
      <td class="wf-num-cell ${tone(dp, true)}">${row.d_profit == null ? "—" : money(dp, true)}</td>
      <td class="wf-num-cell ${tone(dd, false)}">${row.d_drawdown == null ? "—" : money(dd, true)}</td>
      <td class="wf-num-cell ${tone(dt, true)}">${row.d_trades == null ? "—" : plainNumber(dt, 0, true)}</td>
      <td class="wf-num-cell">${row.trades_per_week == null ? `<small>${esc(plainNumber(row.trades, 0))} trades</small>` : plainNumber(row.trades_per_week, 1)}</td>
      <td class="wf-misses">${misses}</td>
      <td><span class="wf-verdict" title="${esc(meta.label)}"><i></i>${esc(meta.short)}</span></td>
    </tr>`;
  }).join("") : `<tr><td colspan="11" class="wf-empty-row">${ui.changesApi === "missing" ? "Restart the desk to see the change list." : ui.changes ? "No rows match this filter." : "Reading the research notes…"}</td></tr>`;
  setHtml(ui.els["impact-rows"], body);
}

// ---------- toast ----------

let toastTimer = 0;

function toast(text, tone = "") {
  const el = ui.els.toast;
  if (!el) return;
  el.textContent = text;
  el.className = `wf-toast is-show ${tone ? `is-${tone}` : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "wf-toast"; }, 4200);
}

// ---------- public ----------

async function show() {
  ui.section = ui.section || document.querySelector("#workflow");
  if (!ui.section) return;
  const wasVisible = ui.visible;
  ui.visible = true;
  const ready = await ensureBuilt();
  if (!ready || !ui.visible) return;
  startPolling();
  if (!wasVisible) {
    requestAnimationFrame(() => {
      if (!ui.userMoved) fit(false);
      else applyCamera();
      ui.particleKey = "";
      updateLive();
    });
  }
}

function hide() {
  if (!ui.visible) return;
  ui.visible = false;
  stopPolling();
  if (ui.els.particles) ui.els.particles.replaceChildren();
  ui.particleKey = "";
}

function feed(data) {
  if (!data || typeof data !== "object") return;
  if (data.snapshot) ui.live.snapshot = data.snapshot;
  if (data.lab) ui.live.lab = data.lab;
  if (data.book) ui.live.book = data.book;
  ui.live.fedAt = Date.now();
  if (ui.visible && ui.built) scheduleLive();
}

window.workflowDesign = { show, hide, feed };

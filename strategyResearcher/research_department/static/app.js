const stateNames = {
  meeting: "Meeting",
  running: "Backtest running",
  passed_stage_1: "Passed stage 1",
  passed_stage_2: "Passed stage 2",
  passed_stage_3: "Passed stage 3",
  passed_all: "Passed all four stages",
  stopped: "Stopped",
};

const page = document.body.dataset.page || "home";
const phone = document.querySelector("#phone");
const rows = document.querySelector("#rows");
const cards = document.querySelector("#cards");
const successTables = document.querySelector("#success-tables");
const engineRuns = document.querySelector("#engine-runs");
const jobState = document.querySelector("#job-state");
const settingsNote = document.querySelector("#settings-note");
const settingsForm = document.querySelector("#settings");
const runButton = document.querySelector("#run");
const thread = document.querySelector("#thread");
const sourceView = document.querySelector("#script-modal");
const sourceTitle = document.querySelector("#source-title");
const sourceScript = document.querySelector("#source-script");
const whoTitle = document.querySelector("#who");
let engineStamp = "";
let strategyStamp = "";
let successStamp = "";
let runsStamp = "";
let threadStamp = "";
let threadReady = false;
let openSource = "";
let librarySort = "profit";

const reader = page === "researcher"
  ? (new URLSearchParams(location.search).get("who") || "Researcher 1")
  : "";

if (whoTitle && reader) {
  whoTitle.textContent = "Meeting";
  document.title = "Meeting";
}

function money(value) {
  if (value === null || value === undefined || value === "") return "";
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return number.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

function renderReward(reward, settings) {
  const block = document.querySelector("#reward");
  if (!block) return;
  const goal = Number((reward && reward.goal) ?? (settings && settings.success_rate) ?? 0);
  const stamp = JSON.stringify({ reward: reward || null, goal });
  if (stamp === renderReward.stamp) return;
  renderReward.stamp = stamp;
  const current = reward ? reward.win_percentage : null;
  const known = current !== null && current !== undefined && Number.isFinite(Number(current));
  const place = known ? Math.max(0, Math.min(1, Number(current))) : 0;
  const mark = Math.max(0, Math.min(1, Number.isFinite(goal) ? goal : 0));
  const winScale = document.querySelector("#win-scale");
  if (winScale) winScale.dataset.direction = (reward && reward.direction) || "waiting";
  const fill = document.querySelector("#scale-fill");
  const goalMark = document.querySelector("#scale-goal");
  const marker = document.querySelector("#scale-marker");
  const goalLabel = document.querySelector("#scale-goal-label");
  const note = document.querySelector("#reward-note");
  if (fill) fill.style.width = known ? `${place * 100}%` : "0%";
  if (goalMark) goalMark.style.left = `${mark * 100}%`;
  if (marker) {
    marker.hidden = !known;
    marker.style.left = `${place * 100}%`;
  }
  if (goalLabel) goalLabel.textContent = `Goal ${Math.round(mark * 100)}%`;
  if (note) note.textContent = rewardText(reward, mark);
  renderProfit(reward);
}

function renderProfit(reward) {
  const scale = document.querySelector("#profit-scale");
  if (!scale) return;
  const total = Number((reward && reward.profit_total) || 3500);
  const goal = Number((reward && reward.profit_goal) || total / 2);
  const profit = reward ? reward.profit : null;
  const known = profit !== null && profit !== undefined && Number.isFinite(Number(profit));
  const span = total > 0 ? total : 1;
  const ratio = !known || Number(profit) <= 0 ? 0 : Math.min(1, Number(profit) / span);
  const goalRatio = Math.max(0, Math.min(1, goal / span));
  scale.dataset.direction = (reward && reward.profit_direction) || "waiting";
  const fill = document.querySelector("#profit-fill");
  const marker = document.querySelector("#profit-marker");
  const goalMark = document.querySelector("#profit-goal");
  const label = document.querySelector("#profit-goal-label");
  const totalLabel = document.querySelector("#profit-total-label");
  const note = document.querySelector("#profit-note");
  if (fill) fill.style.width = `${ratio * 100}%`;
  if (goalMark) goalMark.style.left = `${goalRatio * 100}%`;
  if (marker) {
    marker.hidden = !known;
    marker.style.left = `${ratio * 100}%`;
  }
  if (label) label.textContent = money(goal);
  if (totalLabel) totalLabel.textContent = money(total);
  if (note) note.textContent = profitText(reward, goal, total);
}

function profitText(reward, goal, total) {
  const target = money(goal);
  const whole = money(total);
  const days = reward && reward.days ? `${reward.days} days` : "the days tested";
  if (!reward || reward.profit === null || reward.profit === undefined) {
    return `The scale starts at $0. The total is ${whole}, $500 times ${days}. The goal is 50% of that total, ${target}.`;
  }
  const amount = money(reward.profit);
  let move = "This is the first reading.";
  if (reward.profit_direction === "toward") move = "The last change moved the profit scale toward the goal.";
  if (reward.profit_direction === "away") move = "The last change moved the profit scale away from the goal.";
  if (reward.profit_direction === "held") move = "The last change left the profit scale in place.";
  const place = reward.profit_at_goal ? " The profit is at the goal." : " The profit is short of the goal.";
  return `Profit ${amount}. The scale starts at $0. Total ${whole}. Goal ${target}, 50% of the total. ${move}${place}`;
}

function rewardText(reward, goal) {
  const target = Math.round(goal * 100);
  if (!reward || reward.win_percentage === null || reward.win_percentage === undefined) {
    return `Goal ${target}%. A higher win percentage moves the scale toward the goal. A lower win percentage moves it away.`;
  }
  const win = Math.round(Number(reward.win_percentage) * 100);
  const profit = Number(reward.profit);
  const profitText = Number.isFinite(profit)
    ? ` Profit ${money(profit)}. ${reward.profitable ? "The script is profitable." : "The script is not profitable."}`
    : "";
  let move = "This is the first reading.";
  if (reward.direction === "toward") move = "The last change moved the scale toward the goal.";
  if (reward.direction === "away") move = "The last change moved the scale away from the goal.";
  if (reward.direction === "held") move = "The last change left the scale in place.";
  const place = reward.at_goal ? " The win percentage is at the goal." : " The win percentage is short of the goal.";
  return `Win rate ${win}%. Goal ${target}%. ${move}${place}${profitText}`;
}

function percent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return `${Math.round(number * 100)}%`;
}

function oneLine(text) {
  return String(text || "").replace(/^Thinking\.\s*/, "").replace(/\s+/g, " ").trim();
}

function lineFor(job, who) {
  const messages = (job && job.feed && job.feed.chats && job.feed.chats[who]) || [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.kind === "thinking") {
      const line = oneLine(speak(message.text));
      if (line && line !== "Thinking.") return line;
    }
  }
  const activity = (job && job.feed && job.feed.activity) || [];
  const match = activity.find((item) => item.who === who);
  if (match) {
    const line = oneLine(match.text);
    if (line) return line;
  }
  if (who === "Manager" && job && job.feed && job.feed.manager) {
    return oneLine(speak(job.feed.manager));
  }
  return "Waiting.";
}

function fillSettings(settings) {
  document.querySelector("#success-rate").value = Math.round(Number(settings.success_rate) * 100);
  document.querySelector("#attempts").value = settings.attempts;
  document.querySelector("#daily-target").value = settings.daily_target;
  document.querySelector("#daily-loss").value = settings.daily_loss;
  document.querySelector("#baseline-risk").value = settings.baseline_risk;
}

function showProgress(job, worker, markets) {
  if (!jobState) return;
  const names = (markets && markets.length) ? markets.join(", ") : "ES, NQ, MNQ";
  const stage = !job ? "Waiting for a script" : (stateNames[job.state] || job.state);
  const marketLine = document.querySelector("#markets");
  if (marketLine) marketLine.textContent = names;
  if (worker && worker.running) {
    jobState.textContent = `${worker.file || "the learning folder"} · ${names} · ${stage}`;
    return;
  }
  jobState.textContent = `${names} · ${stage}`;
}

function renderPeople(job) {
  document.querySelectorAll(".person-line").forEach((node) => {
    const next = lineFor(job, node.dataset.who);
    if (node.textContent !== next) node.textContent = next;
  });
}

function paragraphsOf(text) {
  let raw = String(text || "").replace(/^Thinking\.\s*/, "").trim();
  if (raw && !/^[A-Z{"\[]/.test(raw)) {
    const cut = raw.match(/[.!?]\s+([\s\S]{40,})/);
    if (cut) raw = cut[1].trim();
  }
  if (!raw || raw === "Thinking.") return [];
  const blocks = raw.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean);
  if (blocks.length > 1) return blocks;
  const sentences = raw.match(/[^.!?]+[.!?]+(?:\s|$)|[^.!?]+$/g) || [raw];
  const groups = [];
  let bucket = [];
  for (const sentence of sentences) {
    const piece = sentence.trim();
    if (!piece) continue;
    bucket.push(piece);
    if (bucket.length === 3) {
      groups.push(bucket.join(" "));
      bucket = [];
    }
  }
  if (bucket.length) groups.push(bucket.join(" "));
  return groups;
}

const MEETING = ["Researcher 1", "Researcher 2", "Researcher 3", "Manager"];

function embeddedObject(text) {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    const data = JSON.parse(text.slice(start, end + 1));
    if (!data || typeof data !== "object" || Array.isArray(data)) return null;
    return {
      before: text.slice(0, start).trim(),
      data,
      after: text.slice(end + 1).trim(),
    };
  } catch (_error) {
    return null;
  }
}

function voiceOf(data) {
  if (Object.prototype.hasOwnProperty.call(data, "choice")) {
    const names = ["Researcher 1", "Researcher 2", "Researcher 3"];
    const index = Number(data.choice);
    const name = Number.isInteger(index) && names[index] ? names[index] : "that proposal";
    return [`I'll take ${name}'s proposal.`];
  }
  if (!data.variable) return [];
  const down = String(data.direction || "").toLowerCase() === "down";
  const sentences = [`${down ? "Lower" : "Raise"} ${data.variable} to ${data.value}.`];
  const why = String(data.justification || "").trim();
  if (why) sentences.push(why);
  else {
    if (data.consistent_day) sentences.push("That should make the $500 day more consistent.");
    if (data.higher_win_rate) sentences.push("That should raise the win percentage.");
  }
  return sentences;
}

function messageParagraphs(text) {
  const raw = String(text || "").replace(/^Thinking\.\s*/, "").trim();
  if (!raw || raw === "Thinking.") return [];
  const parsed = embeddedObject(raw);
  if (parsed) {
    const chunks = [];
    if (parsed.before) chunks.push(...paragraphsOf(parsed.before));
    chunks.push(...voiceOf(parsed.data));
    if (parsed.after) chunks.push(...paragraphsOf(parsed.after));
    return chunks.filter(Boolean);
  }
  if (raw.startsWith("{") && raw.endsWith("}")) return [];
  return paragraphsOf(raw);
}

function speak(text) {
  return messageParagraphs(text).join(" ");
}

function turnsOf(messages) {
  const turns = [];
  let current = [];
  for (const message of messages || []) {
    current.push(message);
    if (message.kind === "reply" && !message.draft) {
      turns.push(current);
      current = [];
    }
  }
  if (current.length) turns.push(current);
  return turns;
}

function meetingMessages(job) {
  const chats = (job && job.feed && job.feed.chats) || {};
  const turns = MEETING.map((name) => turnsOf(chats[name]));
  const rounds = Math.max(0, ...turns.map((items) => items.length));
  const messages = [];
  for (let round = 0; round < rounds; round += 1) {
    MEETING.forEach((name, index) => {
      for (const message of turns[index][round] || []) {
        messages.push({ ...message, who: name });
      }
    });
  }
  return messages;
}

function speakerClass(who) {
  return `speaker-${String(who || "researcher").toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
}

function renderThread(job) {
  if (!thread) return;
  const messages = meetingMessages(job);
  const stamp = JSON.stringify(messages);
  if (stamp === threadStamp) return;
  const previous = thread.scrollTop;
  const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
  threadStamp = stamp;
  thread.innerHTML = "";
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "thread-empty";
    empty.textContent = "The meeting has not started.";
    thread.append(empty);
    return;
  }
  messages.forEach((message, index) => {
    const kind = message.kind === "content" ? "reply" : (message.kind || (message.draft ? "thinking" : "reply"));
    const paragraphs = messageParagraphs(message.text);
    if (!paragraphs.length) return;
    const article = document.createElement("article");
    const who = message.who || reader || "Researcher";
    article.className = `bubble ${speakerClass(who)} ${kind}${message.draft ? " draft" : ""}`;
    if (who === reader) article.classList.add("selected");
    const label = document.createElement("p");
    label.className = "bubble-label";
    const name = document.createElement("span");
    name.className = "who";
    name.textContent = who;
    label.append(name);
    if (kind === "thinking") {
      const role = document.createElement("span");
      role.className = "role";
      role.textContent = message.draft ? "thinking now" : "thinking";
      label.append(role);
    }
    article.append(label);
    const body = document.createElement("div");
    body.className = "bubble-body";
    for (const paragraph of paragraphs) {
      const item = document.createElement("p");
      item.textContent = paragraph;
      body.append(item);
    }
    article.append(body);
    if (index === messages.length - 1 && message.draft) article.dataset.live = "true";
    thread.append(article);
  });
  if (!threadReady || nearBottom) thread.scrollTop = thread.scrollHeight;
  else thread.scrollTop = previous;
  threadReady = true;
}

function runCells(run) {
  return [
    run.script,
    run.instrument,
    run.change,
    run.days,
    percent(run.success_rate),
    percent(run.win_percentage),
    money(run.profit),
    money(run.worst_day),
    run.trade_count,
    run.quiet_sessions,
    run.attempt,
  ];
}

function fillRunBody(body, runs, empty) {
  body.innerHTML = "";
  if (!runs || !runs.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 11;
    td.textContent = empty;
    tr.append(td);
    body.append(tr);
    return;
  }
  for (const run of runs) {
    const tr = document.createElement("tr");
    for (const value of runCells(run)) {
      const td = document.createElement("td");
      td.textContent = value ?? "";
      tr.append(td);
    }
    body.append(tr);
  }
}

function runTable(title, runs, empty) {
  const block = document.createElement("section");
  block.className = "run-block";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const labels = ["Script", "Instrument", "Change", "Days", "Days at $500", "Win percentage", "Profit", "Worst day", "Trades", "No trade", "Attempt"];
  const row = document.createElement("tr");
  for (const label of labels) {
    const th = document.createElement("th");
    th.textContent = label;
    row.append(th);
  }
  head.append(row);
  const body = document.createElement("tbody");
  fillRunBody(body, runs, empty);
  table.append(head, body);
  wrap.append(table);
  block.append(heading, wrap);
  return block;
}

function renderSuccesses(successes) {
  if (!successTables) return;
  const stamp = JSON.stringify(successes || {});
  if (stamp === successStamp) return;
  successStamp = stamp;
  successTables.innerHTML = "";
  const stages = [["7", "7 days"], ["30", "30 days"], ["90", "90 days"], ["365", "365 days"]];
  for (const [days, title] of stages) {
    const runs = (successes && successes[days]) || [];
    successTables.append(runTable(title, runs, `No successful ${title.replace(" days", "-day")} run yet.`));
  }
}

function renderEngineRuns(engines, runs) {
  if (!engineRuns) return;
  const stamp = JSON.stringify({ engines, runs });
  if (stamp === runsStamp) return;
  runsStamp = stamp;
  engineRuns.innerHTML = "";
  for (const engine of engines || []) {
    const history = (runs && runs[String(engine.id)]) || [];
    engineRuns.append(runTable(
      `${engine.timeframe} engine ${engine.id} · last 20`,
      history,
      "No runs yet.",
    ));
  }
}

function showSource(item) {
  if (!sourceView || !sourceScript) return;
  const requestId = item.id || scriptLabel(item);
  openSource = requestId;
  sourceTitle.textContent = scriptLabel(item);
  sourceScript.value = item.source || "Loading the script.";
  sourceView.hidden = false;
  const copyButton = document.querySelector("#copy-script");
  if (copyButton) copyButton.textContent = "Copy";
  if (item.source) return;
  const params = new URLSearchParams({ file: item.file || "", change: item.change || "" });
  fetch("/library/text?" + params.toString())
    .then((response) => response.json())
    .then((body) => {
      if (openSource !== requestId) return;
      sourceScript.value = body.source || body.reason || "That script is not on disk.";
      sourceScript.setSelectionRange(0, 0);
    })
    .catch(() => {
      if (openSource !== requestId) return;
      sourceScript.value = "That script could not be loaded.";
    });
}

function closeSource() {
  openSource = "";
  if (sourceView) sourceView.hidden = true;
}

async function copySource() {
  if (!sourceScript) return;
  const text = sourceScript.value;
  const copyButton = document.querySelector("#copy-script");
  try {
    await navigator.clipboard.writeText(text);
    if (copyButton) copyButton.textContent = "Copied";
  } catch (_error) {
    sourceScript.focus();
    sourceScript.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_ignored) {
      copied = false;
    }
    if (copyButton) copyButton.textContent = copied ? "Copied" : "Press Ctrl+C";
  }
}

function scriptLabel(item) {
  return item.file || item.script || "Script";
}

function resultKey(item) {
  return [item.script || item.file, item.instrument, item.timeframe, item.days, item.change].join("|");
}

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : Number.NEGATIVE_INFINITY;
}

function collectResults(desk, met) {
  const rows = [];
  const seen = new Set();
  function add(item) {
    if (!item) return;
    const key = resultKey(item);
    if (seen.has(key)) return;
    seen.add(key);
    rows.push(item);
  }
  for (const item of (desk && desk.results) || []) add(item);
  for (const item of (desk && desk.strategies) || []) add(item);
  for (const item of met || []) add(item);
  const runs = (desk && desk.runs) || {};
  for (const list of Object.values(runs)) {
    for (const item of list || []) add(item);
  }
  rows.sort((left, right) => {
    const trades = (item) => (Number(item.trade_count) >= 3 ? 1 : 0);
    if (librarySort === "win") {
      return trades(right) - trades(left) || finite(right.win_percentage) - finite(left.win_percentage) || finite(right.profit) - finite(left.profit);
    }
    return finite(right.profit) - finite(left.profit) || finite(right.win_percentage) - finite(left.win_percentage);
  });
  return rows;
}

function renderStrategies(strategies) {
  if (!rows) return;
  const note = document.querySelector("#library-note");
  if (note) {
    note.textContent = librarySort === "win" ? "Highest win percentage is first." : "Highest profit is first.";
  }
  const stamp = librarySort + JSON.stringify(strategies || []);
  if (stamp === strategyStamp) return;
  strategyStamp = stamp;
  rows.innerHTML = "";
  if (!strategies || !strategies.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 12;
    td.textContent = "No script result yet.";
    tr.append(td);
    rows.append(tr);
    if (sourceView) sourceView.hidden = true;
    return;
  }
  strategies.forEach((item, index) => {
    const tr = document.createElement("tr");
    if (index === 0) tr.className = "best";
    const name = document.createElement("td");
    name.textContent = scriptLabel(item);
    if (index === 0) {
      const mark = document.createElement("span");
      mark.className = "best-mark";
      mark.textContent = "Best";
      name.append(mark);
    }
    tr.append(name);
    const cells = [
      item.instrument,
      item.timeframe,
      item.change,
      item.days,
      percent(item.success_rate),
      percent(item.win_percentage),
      money(item.profit),
      money(item.worst_day),
      item.trade_count,
      item.attempt,
    ];
    for (const value of cells) {
      const td = document.createElement("td");
      td.textContent = value ?? "";
      tr.append(td);
    }
    const action = document.createElement("td");
    action.className = "delete-col";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteStrategy(item.id);
    });
    action.append(remove);
    tr.append(action);
    tr.addEventListener("click", () => showSource(item));
    rows.append(tr);
  });
}

function engineLabel(engine) {
  const market = engine.instrument ? `${engine.instrument} · ` : "";
  if (!engine.free || engine.status === "Working") {
    return `${market}Working · ${engine.script || "backtest"}`;
  }
  if (engine.result) {
    return `${market}Finished · ${engine.script || "backtest"}`;
  }
  if (engine.status) {
    return engine.instrument ? `${market}${engine.status}` : engine.status;
  }
  return "Free";
}

function renderEngines(engines) {
  if (!cards) return;
  const stamp = JSON.stringify(engines || []);
  if (stamp === engineStamp) return;
  engineStamp = stamp;
  cards.innerHTML = "";
  for (const engine of engines || []) {
    const card = document.createElement("article");
    card.className = "card";
    const head = document.createElement("div");
    head.className = "card-head";
    const title = document.createElement("h3");
    title.textContent = `${engine.timeframe} engine ${engine.id}`;
    head.append(title);
    if (!engine.original) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => removeEngine(engine.id));
      head.append(remove);
    }
    const script = document.createElement("p");
    script.className = "script";
    script.textContent = engineLabel(engine);
    card.append(head, script);
    const result = engine.result;
    if (result) {
      const facts = document.createElement("dl");
      const pairs = [
        ["Instrument", result.instrument || engine.instrument],
        ["Days", result.days],
        ["Success", percent(result.success_rate)],
        ["Win rate", percent(result.win_percentage)],
        ["Profit", money(result.profit)],
        ["Worst day", money(result.worst_day)],
        ["Trades", result.trade_count],
        ["No trade", result.quiet_sessions],
      ];
      for (const [label, value] of pairs) {
        const dt = document.createElement("dt");
        dt.textContent = label;
        const dd = document.createElement("dd");
        dd.textContent = value ?? "";
        facts.append(dt, dd);
      }
      card.append(facts);
    }
    cards.append(card);
  }
}

const hiddenMetKey = "research-hidden-met";

async function metRows() {
  if (metRows.cache) return metRows.cache;
  try {
    const response = await fetch("/static/met-win-rate.json?v=11");
    metRows.cache = response.ok ? await response.json() : [];
  } catch (_error) {
    metRows.cache = [];
  }
  if (!Array.isArray(metRows.cache)) metRows.cache = [];
  return metRows.cache;
}
metRows.cache = null;

function visibleMet(rows) {
  let hidden = [];
  try {
    hidden = JSON.parse(localStorage.getItem(hiddenMetKey) || "[]");
  } catch (_error) {
    hidden = [];
  }
  const gone = new Set(hidden);
  return (rows || []).filter((item) => item && item.id && !gone.has(item.id));
}

async function readDesk() {
  const response = await fetch("/desk");
  if (!response.ok) return;
  const desk = await response.json();
  if (settingsForm && document.activeElement.form !== settingsForm) {
    fillSettings(desk.settings);
  }
  if (runButton) {
    const running = Boolean(desk.worker && desk.worker.running);
    runButton.dataset.running = running ? "true" : "false";
    runButton.textContent = running ? "Stop" : "Start";
  }
  if (phone) {
    if (desk.phone) {
      phone.textContent = `On your phone: ${desk.phone}`;
      phone.href = desk.phone;
    } else {
      phone.textContent = "";
      phone.removeAttribute("href");
    }
  }
  showProgress(desk.job, desk.worker, desk.markets);
  renderReward(desk.reward, desk.settings);
  renderPeople(desk.job);
  renderThread(desk.job);
  const met = visibleMet(await metRows());
  const known = new Set((desk.strategies || []).map((item) => item.id));
  const added = met.filter((item) => !known.has(item.id));
  const successes = Object.assign({}, desk.successes || {});
  const seven = [...(successes["7"] || [])];
  const seen = new Set(seven.map((item) => `${item.instrument}|${item.timeframe}|${item.change}|${item.win_percentage}`));
  for (const item of added) {
    const key = `${item.instrument}|${item.timeframe}|${item.change}|${item.win_percentage}`;
    if (!seen.has(key)) seven.unshift(item);
  }
  successes["7"] = seven;
  renderSuccesses(successes);
  renderStrategies(collectResults(desk, [...(desk.strategies || []), ...added]));
  renderEngines(desk.engines);
  renderEngineRuns(desk.engines, desk.runs);
}

async function removeEngine(id) {
  await fetch(`/engines/${id}`, { method: "DELETE" });
  engineStamp = "";
  await readDesk();
}

async function deleteStrategy(id) {
  const path = String(id).startsWith("result-") ? `/catalog/${id}` : `/strategies/${id}`;
  const response = await fetch(path, { method: "DELETE" });
  if (response.status === 404) {
    let hidden = [];
    try {
      hidden = JSON.parse(localStorage.getItem(hiddenMetKey) || "[]");
    } catch (_error) {
      hidden = [];
    }
    hidden.push(id);
    localStorage.setItem(hiddenMetKey, JSON.stringify(hidden));
    strategyStamp = "";
    successStamp = "";
  }
  if (openSource === id) closeSource();
  strategyStamp = "";
  await readDesk();
}

if (settingsForm) {
  settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      success_rate: Number(document.querySelector("#success-rate").value) / 100,
      attempts: Number(document.querySelector("#attempts").value),
      daily_target: Number(document.querySelector("#daily-target").value),
      daily_loss: Number(document.querySelector("#daily-loss").value),
      baseline_risk: Number(document.querySelector("#baseline-risk").value),
    };
    const response = await fetch("/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const saved = await response.json();
    fillSettings(saved);
    settingsNote.textContent = "Settings saved.";
    await readDesk();
  });
}

const addEngine = document.querySelector("#add-engine");
if (addEngine) {
  addEngine.addEventListener("submit", async (event) => {
    event.preventDefault();
    await fetch("/engines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ timeframe: document.querySelector("#timeframe").value }),
    });
    engineStamp = "";
    await readDesk();
  });
}

function chooseSort(mode) {
  librarySort = mode;
  strategyStamp = "";
  const profitButton = document.querySelector("#sort-profit");
  const winButton = document.querySelector("#sort-win");
  if (profitButton) profitButton.setAttribute("aria-pressed", mode === "profit" ? "true" : "false");
  if (winButton) winButton.setAttribute("aria-pressed", mode === "win" ? "true" : "false");
  readDesk();
}

const sortProfit = document.querySelector("#sort-profit");
const sortWin = document.querySelector("#sort-win");
if (sortProfit) sortProfit.addEventListener("click", () => chooseSort("profit"));
if (sortWin) sortWin.addEventListener("click", () => chooseSort("win"));

const copyScript = document.querySelector("#copy-script");
const closeScript = document.querySelector("#close-script");
if (copyScript) copyScript.addEventListener("click", () => copySource());
if (closeScript) closeScript.addEventListener("click", closeSource);
if (sourceView) {
  sourceView.addEventListener("click", (event) => {
    if (event.target === sourceView) closeSource();
  });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && sourceView && !sourceView.hidden) closeSource();
});

if (runButton) {
  runButton.addEventListener("click", async () => {
    const running = runButton.dataset.running === "true";
    await fetch(running ? "/worker/stop" : "/worker/start", { method: "POST" });
    await readDesk();
  });
}

readDesk();
setInterval(readDesk, 1000);

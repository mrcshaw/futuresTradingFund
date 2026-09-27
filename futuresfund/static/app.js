const state = {
  channel: "headquarters",
  roster: [],
  snapshot: { messages: [], agent_status: {}, reports: {}, run: null, activity: {} },
  book: { positions: [], orders: [], settings: {} },
  lab: { messages: [], trials: [], accepted: [], scrapped: [], status: "idle" },
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
      const key = agent.id === "headquarters" || agent.id === "book" || agent.id === "rnd" || agent.id === "engine" ? agent.id : agent.name;
      const active = state.channel === key;
      const dot = key === "headquarters" || key === "book" || key === "rnd" || key === "engine" ? "" : `<span class="dot ${statusOf(agent.name)}"></span>`;
      const task = key !== "headquarters" && key !== "book" && key !== "rnd" && key !== "engine" && statusOf(agent.name) === "working"
        ? `<small>${state.snapshot.activity?.task || "Working now"}</small>` : "";
      return `<button class="channel ${active ? "active" : ""}" data-channel="${key}" data-team="${group.team}" data-role="${agent.role || ""}">${dot}<span># ${agent.name.toLowerCase()}${task}</span></button>`;
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
  };
  const current = pages[state.channel] || state.roster.find((agent) => agent.name === state.channel);
  const deskPage = state.channel === "book" || state.channel === "engine" || state.channel === "rnd";
  document.querySelector("#feed").hidden = deskPage;
  const lead = state.book.lead;
  const leadEl = document.querySelector("#lead");
  leadEl.hidden = state.channel !== "headquarters";
  if (lead && lead.pine) {
    document.querySelector("#lead-title").textContent = lead.title || "Lead strategy";
    document.querySelector("#lead-note").textContent =
      `${lead.trades ?? ""} trades, profit ${lead.net_profit ?? ""}, drawdown ${lead.max_drawdown ?? ""}. ${lead.formula || ""}`;
    document.querySelector("#lead-pine").textContent = lead.pine;
  } else {
    document.querySelector("#lead-title").textContent = "No strategy script yet";
    document.querySelector("#lead-note").textContent = "The most profitable rule the researchers have cleared stays here. Every desk can read this script.";
    document.querySelector("#lead-pine").textContent = "";
  }
  document.querySelector("#book").hidden = state.channel !== "book";
  document.querySelector("#engine").hidden = state.channel !== "engine";
  document.querySelector("#rnd").hidden = state.channel !== "rnd";
  document.querySelector("#composer").hidden = state.channel !== "headquarters";
  titleEl.textContent = `# ${state.channel.toLowerCase()}`;
  teamEl.textContent = current?.team || "Floor";
  roleEl.textContent = current?.role || "Every desk, in one feed.";

  const run = state.snapshot.run;
  const activity = state.snapshot.activity || {};
  stopBtn.hidden = !run || run.status !== "running";
  if (!run || run.status !== "running") {
    const label = run.contract || run.ticker || "";
    pillEl.textContent = label ? `${run.status} · ${label}` : run.status;
    nowText.textContent = run?.status === "stopped"
      ? `Stopped ${label}. The strategy was not changed.`
      : run?.status === "done"
        ? "The last meeting finished. The engine page lists the tests the developer ran."
        : "Upload the TradingView bar file, then develop the strategy.";
  } else {
    pillEl.textContent = `Working ${run.contract || run.ticker || ""}`;
    const who = activity.agent ? `${activity.agent} — ` : "";
    nowText.textContent = `${who}${activity.task || "Working"} · ${run.ticker}`;
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
    || `<p class="chat-line">Ask the portfolio manager about the book. Chat does not place an order.</p>`;
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
    `Prop accounts: ${accounts}. CrossTrade ${settings.crosstrade_configured ? "is configured" : "still needs a URL and key"}. `
    + "TradingView calls /hooks/tradingview/YOUR_TOKEN on a public address. This machine's localhost is not reachable from TradingView.";
  const strategy = state.book.strategy;
  const card = document.querySelector("#strategy-card");
  const rows = strategy?.strategies || [];
  if (!strategy) {
    card.textContent = "No strategy yet. Upload a TradingView bar file with the account size and profit target.";
    document.querySelector("#strategy-list").innerHTML = "";
  } else {
    const active = rows.filter((item) => item.active).map((item) => item.title);
    if (!rows.length) {
      card.textContent = `${strategy.timeframe} ${strategy.contract} for ${strategy.account}. ${strategy.stance}. ${strategy.formula || ""}`;
    } else {
      card.textContent = `${strategy.timeframe} ${strategy.contract} for ${strategy.account}. `
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
account=${strategy?.account || "YOUR_PROP_ACCOUNT"};
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
    run: run.status,
    ticker: run.ticker,
    status: state.snapshot.agent_status,
    lab: `${state.lab.status}:${state.lab.attempts}:${(state.lab.trials || []).length}:${state.lab.running}`,
    orders: (state.book.orders || []).length,
    positions: (state.book.positions || []).length,
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
  const feed = document.querySelector("#rnd-feed");
  if (feed) {
    const notes = lab.messages || [];
    feed.innerHTML = notes.slice().reverse().map((note) => `<article class="message"><header><strong></strong><time></time></header><p></p></article>`).join("")
      || `<p class="hint">No research notes yet. Start a search and the indicator researcher, quantitative researcher, and developer will post here.</p>`;
    feed.querySelectorAll(".message").forEach((node, index) => {
      const note = notes.slice().reverse()[index];
      if (!note) return;
      node.querySelector("strong").textContent = note.author;
      node.querySelector("time").textContent = note.time || "";
      node.querySelector("p").textContent = note.text || "";
    });
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

stopBtn.addEventListener("click", async () => {
  await fetch("/api/runs/stop", { method: "POST" });
  state.paint = "";
  refresh();
});

document.querySelector("#lab-run").addEventListener("click", runLab);
document.querySelector("#rnd-run").addEventListener("click", runLab);

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
  await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  state.paint = "";
  refresh();
});

refresh();
setInterval(refresh, 1200);

"""Architecture and startup for the futures desk.

The desk is a local FastAPI process. It studies Pine strategies, takes
TradingView alerts, and sends orders through CrossTrade. Research does not
start by itself. The user presses Start.

Run ``pydoc futuresfund.architecture`` for this page.


Architecture
------------

The desk is six layers. Each layer calls the one below it. A lower layer
does not reach up to start research or send mail.

1. Edge
    ``server.py`` is the process. It serves the headquarters page, the
    JSON API, and ``POST /hooks/tradingview/{token}``. The browser in
    ``futuresfund/static`` is the only UI. Chat posts to one desk, and
    that desk may hand one written report to another desk.

2. Agents
    ``roster.py`` names the desks. ``crew.py`` gives each desk its own
    thread and its own model. ``board.py`` is the in-memory feed, status,
    and report each desk writes. ``llm.py`` is the model client. A
    meeting passes a written report from one desk to the next. It does
    not debate a live trade.

3. Research
    ``learn.py`` reads Pine files only. It does not create strategy
    ``.py`` files. Four warm PineForge containers in ``pineforge_engine.py``
    stay running. The next test sends new inputs into the container that
    is already running, and each bar file is written once. Engine 1 is
    the 2-minute chart and belongs to the 2min chart developer. Engine 2 is the 5-minute chart and belongs
    to the 5 min chart developer. Engine 3 is the 15-minute chart only
    and belongs to the 15 min chart developer. One chart researcher reads
    all three studies. Engine 4 runs a newly written script once, at its
    original settings, before those studies start. A script is filed only
    after each of those three charts has finished on every research
    instrument. ``library.py`` files a profitable result under ES or
    Gold. ``discuss.py`` holds the 8:00am and 5:00pm meetings.

4. Floor
    ``session.py`` handles an alert after ingestion. ``trade_manager.py``
    turns the alert into place, hold, or close. A sell while flat opens a
    short. A later buy closes that short and leaves the book flat. The
    same is true in reverse. An opposite alert does not open the other
    side. ``crosstrade.py`` builds the webhook from the stored key and
    posts it when dry run is off. The account on the alert does not block
    the order. The order uses the configured account when the alert names
    a different one.

5. Book and charts
    ``book.py`` keeps positions, closed trades, and equity per account.
    The headquarters bar shows one card for each account: P&L, drawdown,
    and equity. ``ingestion.py`` writes a live candle on every price
    alert. ``chart_feed.py`` and ``chart_view.py`` fold the 1-minute
    history in ``chartData/es`` and ``chartData/qo`` into the timeframe
    on screen. A forming candle updates on each alert.

6. Settings
    ``config.py`` reads ``futuresTradingFund/.env`` only. Secrets stay
    there: the CrossTrade key, the TradingView token, and the mail
    password. The book, bars, and lab notes are JSON files under the
    user's ``.futuresfund`` directory.


Startup
-------

1. The virtual environment is the hedge-fund environment. The process
   needs ``futuresTradingFund`` and ``tradingEngine`` on ``PYTHONPATH``.
2. From ``futuresTradingFund``::

       python -m uvicorn futuresfund.server:app --host 127.0.0.1 --port 8788

3. Importing ``server`` calls ``config.load_env()``. That loads this
   desk's ``.env`` over any same-named variables already in the
   environment.
4. Uvicorn binds ``127.0.0.1:8788`` and serves ``/``.
5. The startup hook then does three things:
    - ``session.scheduler`` starts on a daemon thread and watches the
      New York clock for the 8:00am and 5:00pm meetings.
    - The NinjaTrader listener starts.
    - A boot thread loads the 15-minute ES file when the bar store is
      empty, posts the developer, systems, and compliance notes, builds
      one model client per agent, and starts the crew.
6. The boot message says processing is stopped. Pine research stays
   stopped until the user presses Start. Alerts are still accepted.
7. Pressing Start runs ``learn.py``. Stop ends that loop. Neither button
   is hidden. Start is disabled while research is running. Stop is
   disabled while it is idle.

Meetings are 8:00am Eastern Monday through Friday and 5:00pm Eastern
Sunday through Friday. Saturday has no meeting. A meeting pauses
learning and does not place an order.

A price alert whose id is ``desk-bar`` records the candle and does not
send an order. Any other alert with a buy or sell is an order. Dry run
builds the webhook and does not post it. With dry run off, CrossTrade
receives the webhook.


Strategy developer
------------------

The chart desks change one input inside a script that already exists.
A strategy developer is a later agent. It writes a new Pine script from
the scripts already in the learning folder. The new file goes into that
folder and is studied like any other script. It does not arm a live
trade. The base model stays the local model already on the desk. These
two write-ups say how to teach it. They do not ask for a model trained
from scratch.

Parthasarathy, Zafar, Khan, and Shahid, "The Ultimate Guide to
Fine-Tuning LLMs from Basics to Breakthroughs", arXiv:2408.13296.

Use the parts of their seven-stage pipeline that fit this desk.

- Dataset preparation. Each example is a pair. The input is one existing
  script, the one change that was measured, and what that change did to
  profit, drawdown, and trade count. The output is the reasoning and
  then one new Pine script. Near-copies of the same idea count once.
  Results from the old engine are left out.
- Model initialisation. Start from the local model. Do not train a new
  base model.
- Instruction prompting first. Put a few of those pairs in the prompt.
  A weight update comes only after prompting is not enough.
- Partial fine-tuning. If a tune is required, use LoRA or QLoRA and
  leave the base weights frozen. The learning folder is too small for
  a full weight update, and a full update can forget the general model.
- Evaluation. Judge the new script on bars that were not used to write
  it. Stop when those later bars get worse. Text scores are not the judge.
- Deployment. A script that compiles is dropped into the learning folder.
  The three chart desks then run it.
- Monitoring. The 120 different results on each chart are the record of
  whether the new script stayed steady.

Their preference methods, DPO and PPO, apply as a ranking. When two
candidate scripts exist, keep the one whose path was steadier. The
research guide is the preference. A larger profit on one chart is not
the label. Their mixture-of-agents note matches the desk we have: the
strategy developer reads the three chart researchers. It does not
replace them.

Personnat, "Large Language Models and How We Train Them", Harvard
University, 2025.
https://dash.harvard.edu/handle/1/42719540

- The test is data the model did not see. A script that only wins on
  the bars used to write it is overfit. PineForge on a later stretch of
  bars is the test. Perplexity and exam benchmarks are not.
- Supervised examples are a prompt and the Pine that should follow.
  The desired answer includes the reasoning before the script.
- Retrieval is the first teaching method. Pull a few strategy cards
  into the prompt. Continued training on the raw folder is a weak fit,
  because the folder is small next to the model's original training
  and a large extra pass can wash out the base behavior.
- Copying the existing scripts is imitation. That policy fails when the
  new script meets a market state the parent scripts never visited.
  After each engine run, add the result to the examples, including the
  runs that got worse. The failures are part of the lesson.
- A person can rank two candidate scripts. A comparison is enough.
  The model does not invent the profit, the drawdown, or the trade count.
  The engine writes those numbers.
"""

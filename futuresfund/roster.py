"""Futures floor. These are the only desks on this firm."""

from __future__ import annotations

ROSTER = [
    {
        "id": "quant_trader",
        "name": "Quantitative Trader",
        "team": "Core Trading",
        "role": "Systematic trader. Builds a futures rule, the engine tests it, and a proven rule can be armed. Executes the second active strategy started, on paper or live.",
        "responsibilities": [
            "Builds a futures rule and has the engine test it",
            "A proven rule can be armed",
            "Executes the second active strategy, on paper or live",
        ],
        "report": "strategy_report",
        "model": "desk-quant-trader",
    },
    {
        "id": "portfolio",
        "name": "Portfolio Manager",
        "team": "Core Trading",
        "role": "Weighs each desk's vote and recommends one rule that cleared the prop account. The user turns it live. Emails the address for viewing this desk on a phone, and emails the new address when the old one expires.",
        "responsibilities": [
            "Weighs each desk's vote",
            "Recommends one rule that cleared the prop account",
            "The user turns that rule live",
            "Emails the phone address, and a new one when the old one expires",
        ],
        "report": "fund_meeting",
        "model": "desk-portfolio",
    },
    {
        "id": "ingestion",
        "name": "Ingestion",
        "team": "Core Trading",
        "role": "Accepts each TradingView alert and adds that bar to the chart. Does not place an order.",
        "responsibilities": [
            "Accepts each TradingView alert",
            "Adds that bar to the chart",
            "Does not place an order",
        ],
        "report": "ingestion_report",
        "model": "desk-ingestion",
        "votes": False,
    },
    {
        "id": "floor",
        "name": "Floor Trader",
        "team": "Core Trading",
        "role": "Executes the first active strategy started: runs its Pine script on each live candle from ingestion and takes its entries and exits, on paper or through the CrossTrade webhook. The stop stays in that script.",
        "responsibilities": [
            "Executes the first active strategy",
            "Runs its Pine script on each live candle",
            "Takes that script's entries and exits, on paper or through CrossTrade",
            "The stop stays in the script",
        ],
        "report": "trader_investment_plan",
        "model": "desk-floor",
    },
    {
        "id": "researcher",
        "name": "Chart researcher",
        "team": "Research",
        "role": "Reads every chart study and chooses the next setting. The objective is to hit the profit target set on headquarters inside the trailing drawdown set there, with the lowest drawdown and then the profit: a change that moves toward the target, or lowers the drawdown once there, is continued; a change that moves away is reversed. The 2-minute, 5-minute, and 15-minute developers bring the results here.",
        "responsibilities": [
            "Reads every chart study and chooses the next setting",
            "Keeps a change that moves toward the profit target or lowers the drawdown",
            "Reverses a change that moves away",
            "The 2-minute, 5-minute, and 15-minute developers bring the results here",
        ],
        "report": "market_report",
        "model": "desk-researcher",
    },
    {
        "id": "card_keeper",
        "name": "Card keeper",
        "team": "Strategy creation",
        "role": "Turns each measured test change from the learning and learned studies into a card the chart strategy developers can read, the changes that moved the result toward the target or lowered the drawdown first.",
        "responsibilities": [
            "Turns each measured test change into a card",
            "Puts changes that moved toward the target, or lowered the drawdown, first",
            "The chart strategy developers read those cards",
        ],
        "report": "card_report",
        "model": "desk-card-keeper",
    },
    {
        "id": "strategy_developer_2",
        "name": "2min strategy developer",
        "team": "Strategy creation",
        "role": "Writes new Pine scripts from scratch for the 2-minute chart the whole time research is running. Learns from new library strategies and from scripts this team already wrote. Engine 4 develops each script.",
        "responsibilities": [
            "Writes new Pine scripts from scratch for the 2-minute chart",
            "Keeps writing while research is running",
            "Learns from library strategies and from scripts this team already wrote",
            "Engine 4 develops each script",
        ],
        "report": "created_script_2",
        "model": "desk-strategy-developer-2",
    },
    {
        "id": "strategy_developer_5",
        "name": "5 min strategy developer",
        "team": "Strategy creation",
        "role": "Writes new Pine scripts from scratch for the 5-minute chart the whole time research is running. Learns from new library strategies and from scripts this team already wrote. Engine 4 develops each script.",
        "responsibilities": [
            "Writes new Pine scripts from scratch for the 5-minute chart",
            "Keeps writing while research is running",
            "Learns from library strategies and from scripts this team already wrote",
            "Engine 4 develops each script",
        ],
        "report": "created_script_5",
        "model": "desk-strategy-developer-5",
    },
    {
        "id": "strategy_developer_15",
        "name": "15 min strategy developer",
        "team": "Strategy creation",
        "role": "Writes new Pine scripts from scratch for the 15-minute chart the whole time research is running. Learns from new library strategies and from scripts this team already wrote. Engine 4 develops each script.",
        "responsibilities": [
            "Writes new Pine scripts from scratch for the 15-minute chart",
            "Keeps writing while research is running",
            "Learns from library strategies and from scripts this team already wrote",
            "Engine 4 develops each script",
        ],
        "report": "created_script_15",
        "model": "desk-strategy-developer-15",
    },
    {
        "id": "script_checker",
        "name": "Script checker",
        "team": "Strategy creation",
        "role": "Checks that the new script has a version line, an entry, an exit, and a protective stop on every entry before engine 4 develops it. Checks the Trading Analyst's description of a script and sends it back when that description does not match the code.",
        "responsibilities": [
            "Checks the version line",
            "Checks the entry, the exit, and a protective stop on every entry",
            "Does this before engine 4 develops the script",
            "Checks the description of the script and sends it back when it does not match the code",
        ],
        "report": "script_check",
        "model": "desk-script-checker",
    },
    {
        "id": "creation_tester",
        "name": "Creation tester",
        "team": "Strategy creation",
        "role": "Develops each new script on engine 4. A profitable run inside 20 percent of the account is filed for the chart engines. Skip leaves the current script and takes the next one.",
        "responsibilities": [
            "Develops each new script on engine 4",
            "Files a profitable run inside 20 percent of the account for the chart engines",
            "Skip leaves the current script and takes the next one",
        ],
        "report": "creation_test",
        "model": "desk-creation-tester",
    },
    {
        "id": "lesson_writer",
        "name": "Lesson writer",
        "team": "Strategy creation",
        "role": "Records every script a chart strategy developer files, what it was built from, and how the chart study turned out, so the next script learns from it.",
        "responsibilities": [
            "Records every script a chart strategy developer files",
            "Records what it was built from and how the study turned out",
            "The next script learns from that record",
        ],
        "report": "lesson_report",
        "model": "desk-lesson-writer",
    },
    {
        "id": "risk",
        "name": "Risk Manager",
        "team": "Research",
        "role": "Checks the Apex intraday trailing floor, the contract cap, and the daily loss limit before a rule can be discussed.",
        "responsibilities": [
            "Checks the Apex intraday trailing floor",
            "Checks the contract cap",
            "Checks the daily loss limit before a rule can be discussed",
        ],
        "report": "risk_report",
        "model": "desk-risk",
    },
    {
        "id": "analyst",
        "name": "Trading Analyst",
        "team": "Research",
        "role": "Confirms the fill with NinjaTrader and tells the floor trader to close when the halt or the drawdown can liquidate the account. Before a backtest, reads that script and describes how it works.",
        "responsibilities": [
            "Confirms the fill with NinjaTrader",
            "Tells the floor trader to close when the halt or the drawdown can liquidate the account",
            "Reads the script before the backtest and describes how that script works",
            "Describes an average cross only when the script enters on a cross",
        ],
        "report": "investment_plan",
        "model": "desk-analyst",
    },
    {
        "id": "developer",
        "name": "2min chart developer",
        "team": "Technology & Operations",
        "role": "Runs the 2-minute chart on engine 1, looking for the profit target inside the trailing drawdown with the lowest drawdown. Adds a script's entry rule to the engine when that rule is missing.",
        "responsibilities": [
            "Runs the 2-minute chart on engine 1",
            "Looks for the profit target inside the trailing drawdown, with the lowest drawdown",
            "Adds a script's entry rule when that rule is missing",
        ],
        "report": "engine_report",
        "model": "desk-developer",
    },
    {
        "id": "developer_2",
        "name": "5 min chart developer",
        "team": "Technology & Operations",
        "role": "Runs the 5-minute chart on engine 2, looking for the profit target inside the trailing drawdown with the lowest drawdown. Adds a script's entry rule to the engine when that rule is missing.",
        "responsibilities": [
            "Runs the 5-minute chart on engine 2",
            "Looks for the profit target inside the trailing drawdown, with the lowest drawdown",
            "Adds a script's entry rule when that rule is missing",
        ],
        "report": "engine_report_2",
        "model": "desk-developer-2",
    },
    {
        "id": "developer_15",
        "name": "15 min chart developer",
        "team": "Technology & Operations",
        "role": "Runs the 15-minute chart on engine 3, looking for the profit target inside the trailing drawdown with the lowest drawdown. Adds a script's entry rule to the engine when that rule is missing.",
        "responsibilities": [
            "Runs the 15-minute chart on engine 3",
            "Looks for the profit target inside the trailing drawdown, with the lowest drawdown",
            "Adds a script's entry rule when that rule is missing",
        ],
        "report": "engine_report_15",
        "model": "desk-developer-15",
    },
    {
        "id": "systems",
        "name": "Systems Administrator",
        "team": "Technology & Operations",
        "role": "Serves requests made of the desk, including the strategy library, and handles prop-firm mail about the account. Answers why an engine is not running and fixes it. When the learning folder is empty and the engines have no file, moves created strategies into the learning folder, or presses Make new strategies when that folder is empty too, and emails what happened. Keeps the error journal: every error raised while the workflow runs is written down, and the daily error report goes to the Portfolio Manager to email.",
        "responsibilities": [
            "Serves desk requests, including the strategy library",
            "Handles prop-firm mail about the account",
            "Answers why an engine is not running and fixes it",
            "Moves created strategies into the learning folder when that folder is empty and the engines have no file",
            "Presses Make new strategies when created strategies is empty too, and emails what happened",
            "Keeps the error journal and sends the daily error report to the Portfolio Manager",
        ],
        "report": "systems_report",
        "model": "desk-systems",
    },
    {
        "id": "compliance",
        "name": "Compliance & Operations",
        "team": "Technology & Operations",
        "role": "Enforces the trailing drawdown, any daily loss limit the prop firm has stated, and the 4:45pm ET flatten, with the systems administrator.",
        "responsibilities": [
            "Enforces the trailing drawdown",
            "Enforces any daily loss limit the prop firm has stated",
            "Enforces the 4:45pm ET flatten, with the systems administrator",
        ],
        "report": "compliance_report",
        "model": "desk-compliance",
    },
]

CHART_RESEARCHER = "Chart researcher"
CHART_DESKS = (
    {"timeframe": "2m", "label": "2-minute", "developer": "2min chart developer", "strategy_developer": "2min strategy developer", "researcher": CHART_RESEARCHER, "engine": 1},
    {"timeframe": "5m", "label": "5-minute", "developer": "5 min chart developer", "strategy_developer": "5 min strategy developer", "researcher": CHART_RESEARCHER, "engine": 2},
    {"timeframe": "15m", "label": "15-minute", "developer": "15 min chart developer", "strategy_developer": "15 min strategy developer", "researcher": CHART_RESEARCHER, "engine": 3},
)


def chart_desk(timeframe: str) -> dict:
    """The researcher and developer who own this chart."""
    found = next((item for item in CHART_DESKS if item["timeframe"] == timeframe), None)
    if found is None:
        return CHART_DESKS[0]
    return found


TASKS = {
    "Quantitative Trader": "Proposing a rule the engine can backtest",
    "Portfolio Manager": "Choosing which proven rule to arm",
    "Ingestion": "Adding the TradingView alert to the chart",
    "Floor Trader": "Placing and managing the armed order",
    "Chart researcher": "Reading the 2-minute, 5-minute, and 15-minute studies",
    "Card keeper": "Writing a card from each measured change",
    "2min strategy developer": "Writing a new Pine script from scratch for engine 4",
    "5 min strategy developer": "Writing a new Pine script from scratch for engine 4",
    "15 min strategy developer": "Writing a new Pine script from scratch for engine 4",
    "Script checker": "Checking the script and whether its description matches the code",
    "Creation tester": "Developing the next script on engine 4",
    "Lesson writer": "Recording how the created script did so the next one learns from it",
    "Risk Manager": "Checking drawdown against the account",
    "Trading Analyst": "Reading the script before the backtest, then confirming the fill",
    "2min chart developer": "Adding the entry rule, then running the 2-minute chart on engine 1",
    "5 min chart developer": "Adding the entry rule, then running the 5-minute chart on engine 2",
    "15 min chart developer": "Adding the entry rule, then running the 15-minute chart on engine 3",
    "Systems Administrator": "Serving desk requests and keeping the error journal",
    "Compliance & Operations": "Enforcing the prop-account rules",
}

REPORT_OWNER = {agent["report"]: agent["name"] for agent in ROSTER}

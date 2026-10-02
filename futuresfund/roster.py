"""Futures floor. These are the only desks on this firm."""

from __future__ import annotations

ROSTER = [
    {
        "id": "quant_trader",
        "name": "Quantitative Trader",
        "team": "Core Trading",
        "role": "Systematic trader. Builds a futures rule, the engine tests it, and a proven rule can be armed.",
        "report": "strategy_report",
        "model": "desk-quant-trader",
    },
    {
        "id": "portfolio",
        "name": "Portfolio Manager",
        "team": "Core Trading",
        "role": "Weighs each desk's vote and recommends one rule that cleared the prop account. The user turns it live.",
        "report": "fund_meeting",
        "model": "desk-portfolio",
    },
    {
        "id": "ingestion",
        "name": "Ingestion",
        "team": "Core Trading",
        "role": "Accepts each TradingView alert and adds that bar to the chart. Does not place an order.",
        "report": "ingestion_report",
        "model": "desk-ingestion",
        "votes": False,
    },
    {
        "id": "floor",
        "name": "Floor Trader",
        "team": "Core Trading",
        "role": "Executes the buy or sell from the TradingView strategy and builds the CrossTrade webhook. The stop stays in that script.",
        "report": "trader_investment_plan",
        "model": "desk-floor",
    },
    {
        "id": "researcher",
        "name": "Chart researcher",
        "team": "Research",
        "role": "Reads every chart study. The 2-minute, 5-minute, and 15-minute developers bring the 120 results here.",
        "report": "market_report",
        "model": "desk-researcher",
    },
    {
        "id": "card_keeper",
        "name": "Card keeper",
        "team": "Strategy creation",
        "role": "Turns each measured test change into a card the strategy developer can read.",
        "report": "card_report",
        "model": "desk-card-keeper",
    },
    {
        "id": "strategy_developer",
        "name": "Strategy developer",
        "team": "Strategy creation",
        "role": "Writes one new Pine script from the cards. The script differs by one idea.",
        "report": "created_script",
        "model": "desk-strategy-developer",
    },
    {
        "id": "script_checker",
        "name": "Script checker",
        "team": "Strategy creation",
        "role": "Checks that the new script has an entry, an exit, and a version line before the engine runs it.",
        "report": "script_check",
        "model": "desk-script-checker",
    },
    {
        "id": "creation_tester",
        "name": "Creation tester",
        "team": "Strategy creation",
        "role": "Runs a created script on engine 4 for up to 200 attempts and stops when the result is profitable.",
        "report": "creation_test",
        "model": "desk-creation-tester",
    },
    {
        "id": "lesson_writer",
        "name": "Lesson writer",
        "team": "Strategy creation",
        "role": "Adds finished test changes back onto the cards so the strategy developer learns from them.",
        "report": "lesson_report",
        "model": "desk-lesson-writer",
    },
    {
        "id": "risk",
        "name": "Risk Manager",
        "team": "Research",
        "role": "Checks the Apex intraday trailing floor, the contract cap, and the daily loss limit before a rule can be discussed.",
        "report": "risk_report",
        "model": "desk-risk",
    },
    {
        "id": "analyst",
        "name": "Trading Analyst",
        "team": "Research",
        "role": "Confirms the fill with NinjaTrader and tells the floor trader to close when the halt or the drawdown can liquidate the account.",
        "report": "investment_plan",
        "model": "desk-analyst",
    },
    {
        "id": "developer",
        "name": "2min chart developer",
        "team": "Technology & Operations",
        "role": "Runs the 2-minute chart on engine 1.",
        "report": "engine_report",
        "model": "desk-developer",
    },
    {
        "id": "developer_2",
        "name": "5 min chart developer",
        "team": "Technology & Operations",
        "role": "Runs the 5-minute chart on engine 2.",
        "report": "engine_report_2",
        "model": "desk-developer-2",
    },
    {
        "id": "developer_15",
        "name": "15 min chart developer",
        "team": "Technology & Operations",
        "role": "Runs the 15-minute chart on engine 3.",
        "report": "engine_report_15",
        "model": "desk-developer-15",
    },
    {
        "id": "systems",
        "name": "Systems Administrator",
        "team": "Technology & Operations",
        "role": "Handles prop-firm mail about the account and brings those rules to compliance.",
        "report": "systems_report",
        "model": "desk-systems",
    },
    {
        "id": "compliance",
        "name": "Compliance & Operations",
        "team": "Technology & Operations",
        "role": "Enforces the trailing drawdown, any daily loss limit the prop firm has stated, and the 4:45pm ET flatten, with the systems administrator.",
        "report": "compliance_report",
        "model": "desk-compliance",
    },
]

CHART_RESEARCHER = "Chart researcher"
CHART_DESKS = (
    {"timeframe": "2m", "label": "2-minute", "developer": "2min chart developer", "researcher": CHART_RESEARCHER, "engine": 1},
    {"timeframe": "5m", "label": "5-minute", "developer": "5 min chart developer", "researcher": CHART_RESEARCHER, "engine": 2},
    {"timeframe": "15m", "label": "15-minute", "developer": "15 min chart developer", "researcher": CHART_RESEARCHER, "engine": 3},
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
    "Strategy developer": "Writing one new Pine script from the cards",
    "Script checker": "Checking the entry, the exit, and the version line",
    "Creation tester": "Running a created script on engine 4 until it is profitable",
    "Lesson writer": "Adding finished changes back onto the cards",
    "Risk Manager": "Checking drawdown against the account",
    "Trading Analyst": "Confirming the fill and watching the drawdown",
    "2min chart developer": "Running the 2-minute chart on engine 1",
    "5 min chart developer": "Running the 5-minute chart on engine 2",
    "15 min chart developer": "Running the 15-minute chart on engine 3",
    "Systems Administrator": "Reading prop-firm mail",
    "Compliance & Operations": "Enforcing the prop-account rules",
}

REPORT_OWNER = {agent["report"]: agent["name"] for agent in ROSTER}

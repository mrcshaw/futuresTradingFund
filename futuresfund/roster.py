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
        "name": "2 min researcher",
        "team": "Research",
        "role": "Chooses the one change for the 2-minute chart and reads the result from the 2min chart developer.",
        "report": "market_report",
        "model": "desk-researcher",
    },
    {
        "id": "indicator",
        "name": "5 min researcher",
        "team": "Research",
        "role": "Chooses the one change for the 5-minute chart and reads the result from the 5 min chart developer.",
        "report": "indicator_report",
        "model": "desk-indicator",
    },
    {
        "id": "researcher_15",
        "name": "15 min researcher",
        "team": "Research",
        "role": "Chooses the one change for the 15-minute chart and reads the result from the 15 min chart developer.",
        "report": "chart_15_report",
        "model": "desk-researcher-15",
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
        "role": "Runs the 2-minute chart on engine 1. That PineForge container stays warm.",
        "report": "engine_report",
        "model": "desk-developer",
    },
    {
        "id": "developer_2",
        "name": "5 min chart developer",
        "team": "Technology & Operations",
        "role": "Runs the 5-minute chart on engine 2. That PineForge container stays warm.",
        "report": "engine_report_2",
        "model": "desk-developer-2",
    },
    {
        "id": "developer_15",
        "name": "15 min chart developer",
        "team": "Technology & Operations",
        "role": "Runs the 15-minute chart on engine 3 only. That PineForge container stays warm.",
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

CHART_DESKS = (
    {"timeframe": "2m", "label": "2-minute", "developer": "2min chart developer", "researcher": "2 min researcher", "engine": 1},
    {"timeframe": "5m", "label": "5-minute", "developer": "5 min chart developer", "researcher": "5 min researcher", "engine": 2},
    {"timeframe": "15m", "label": "15-minute", "developer": "15 min chart developer", "researcher": "15 min researcher", "engine": 3},
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
    "2 min researcher": "Choosing the next change on the 2-minute chart",
    "5 min researcher": "Choosing the next change on the 5-minute chart",
    "15 min researcher": "Choosing the next change on the 15-minute chart",
    "Risk Manager": "Checking drawdown against the account",
    "Trading Analyst": "Confirming the fill and watching the drawdown",
    "2min chart developer": "Running the 2-minute chart on engine 1",
    "5 min chart developer": "Running the 5-minute chart on engine 2",
    "15 min chart developer": "Running the 15-minute chart on engine 3",
    "Systems Administrator": "Reading prop-firm mail",
    "Compliance & Operations": "Enforcing the prop-account rules",
}

REPORT_OWNER = {agent["report"]: agent["name"] for agent in ROSTER}

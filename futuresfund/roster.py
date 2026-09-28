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
        "name": "Quantitative Researcher",
        "team": "Research",
        "role": "Reads academic papers and factor notes, then asks the developer to test the part the engine can run.",
        "report": "market_report",
        "model": "desk-researcher",
    },
    {
        "id": "indicator",
        "name": "Indicator Researcher",
        "team": "Research",
        "role": "Pulls public chart indicators and asks the developer to change their settings until the backtest works or the idea is scrapped.",
        "report": "indicator_report",
        "model": "desk-indicator",
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
        "name": "Quantitative Developer",
        "team": "Technology & Operations",
        "role": "Owns the fill engine: entries at the close, stops inside the bar, commission, and slippage.",
        "report": "engine_report",
        "model": "desk-developer",
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

TASKS = {
    "Quantitative Trader": "Proposing a rule the engine can backtest",
    "Portfolio Manager": "Choosing which proven rule to arm",
    "Ingestion": "Adding the TradingView alert to the chart",
    "Floor Trader": "Placing and managing the armed order",
    "Quantitative Researcher": "Reading papers and factor notes",
    "Indicator Researcher": "Adjusting a public indicator on the bar file",
    "Risk Manager": "Checking drawdown against the account",
    "Trading Analyst": "Confirming the fill and watching the drawdown",
    "Quantitative Developer": "Checking the fill engine",
    "Systems Administrator": "Reading prop-firm mail",
    "Compliance & Operations": "Enforcing the prop-account rules",
}

REPORT_OWNER = {agent["report"]: agent["name"] for agent in ROSTER}

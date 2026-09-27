"""Futures floor. These are the only desks on this firm."""

from __future__ import annotations

ROSTER = [
    {
        "id": "quant_trader",
        "name": "Quantitative Trader",
        "team": "Core Trading",
        "role": "Systematic trader. Builds a futures rule, the engine tests it, and a proven rule can be armed.",
        "report": "strategy_report",
    },
    {
        "id": "portfolio",
        "name": "Portfolio Manager",
        "team": "Core Trading",
        "role": "Weighs each desk's vote and recommends one rule that cleared the prop account. The user turns it live.",
        "report": "fund_meeting",
    },
    {
        "id": "floor",
        "name": "Floor Trader",
        "team": "Core Trading",
        "role": "Sends the order the armed rule agrees on. One contract, with one tick of slippage and $5.50 a side.",
        "report": "trader_investment_plan",
    },
    {
        "id": "researcher",
        "name": "Quantitative Researcher",
        "team": "Research",
        "role": "Reads academic papers and factor notes, then asks the developer to test the part the engine can run.",
        "report": "market_report",
    },
    {
        "id": "indicator",
        "name": "Indicator Researcher",
        "team": "Research",
        "role": "Pulls public chart indicators and asks the developer to change their settings until the backtest works or the idea is scrapped.",
        "report": "indicator_report",
    },
    {
        "id": "risk",
        "name": "Risk Manager",
        "team": "Research",
        "role": "Checks the Apex intraday trailing floor, the contract cap, and the daily loss limit before a rule can be discussed.",
        "report": "risk_report",
    },
    {
        "id": "analyst",
        "name": "Trading Analyst",
        "team": "Research",
        "role": "Pre-trade check of the backtest: profit, drawdown, and how many trades it took.",
        "report": "investment_plan",
    },
    {
        "id": "developer",
        "name": "Quantitative Developer",
        "team": "Technology & Operations",
        "role": "Owns the fill engine: entries at the close, stops inside the bar, commission, and slippage.",
        "report": "engine_report",
    },
    {
        "id": "systems",
        "name": "Systems Administrator",
        "team": "Technology & Operations",
        "role": "Watches this machine, the bar file, and the alert path. This desk has no exchange co-location.",
        "report": "systems_report",
    },
    {
        "id": "compliance",
        "name": "Compliance & Operations",
        "team": "Technology & Operations",
        "role": "Flattens the book at 4:45pm ET, 15 minutes before the 5:00pm ET futures halt, until 6:00pm ET.",
        "report": "compliance_report",
    },
]

TASKS = {
    "Quantitative Trader": "Proposing a rule the engine can backtest",
    "Portfolio Manager": "Choosing which proven rule to arm",
    "Floor Trader": "Sending the order the armed rule agreed on",
    "Quantitative Researcher": "Reading papers and factor notes",
    "Indicator Researcher": "Adjusting a public indicator on the bar file",
    "Risk Manager": "Checking drawdown against the account",
    "Trading Analyst": "Checking the backtest before anything is armed",
    "Quantitative Developer": "Checking the fill engine",
    "Systems Administrator": "Checking the bar feed on this machine",
    "Compliance & Operations": "Enforcing the 4:45pm ET flatten",
}

REPORT_OWNER = {agent["report"]: agent["name"] for agent in ROSTER}

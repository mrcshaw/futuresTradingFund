# Futures desk

This is a futures trading firm. The desks are the quantitative trader, portfolio manager, floor trader, researchers, risk manager, trading analyst, developer, systems administrator, and compliance. They discuss a rule, the engine backtests it, and the user turns a proven rule live.

An order is sent by a later TradingView alert, not by the meeting.

1. Export 15-minute bars from TradingView, as far back as the chart will give you, and upload that CSV with the prop account, account size, and profit target.
2. The desk tests moving-average, breakout, and RSI rules on those bars. Contract size is set from 1% of the account against recent range. A rule is armed only when the backtest profit is positive and the drawdown stays inside the account.
3. TradingView then sends an alert on that same timeframe with the price, the open position, and the assigned account. The armed rule places, holds, or closes. Each alert is stored.
4. Research continues until three rules pass risk. The portfolio manager leads the vote at 8:00am ET Monday through Friday, ahead of the 9:30 open, and at 5:00pm ET Sunday through Friday, while the futures market is closed. Saturday has no meeting. After a meeting the portfolio manager writes a report of what the fund has been doing and emails it when SMTP is set in `.env`.

The CrossTrade secret stays in `.env`. The alert must not contain it. The account in the alert must be listed in `PROP_ACCOUNTS` and, once the strategy names an account, must be that account.

`DRY_RUN` starts as true. Matching alerts are shown on the book and are not posted. Set `DRY_RUN=false` after the CrossTrade URL and key are in `.env`.

TradingView cannot call `127.0.0.1`. The webhook URL TradingView uses has to be a public host that forwards to this app.

## Run

From this folder:

```
python -m uvicorn futuresfund.server:app --host 127.0.0.1 --port 8788
```

Open http://127.0.0.1:8788

Copy `.env.example` to `.env` and fill in the CrossTrade URL, the CrossTrade key, the webhook token, and the prop account names.

## TradingView message

```
account=YOUR_PROP_ACCOUNT;
instrument=MES 12-26;
timeframe=15m;
action={{strategy.order.action}};
qty=1;
price={{close}};
position={{strategy.market_position}};
position_size={{strategy.position_size}};
order_type=MARKET;
tif=DAY;
```

`timeframe` may also be TradingView's `{{interval}}` (`15` for 15 minutes). The alert's buy or sell text is not the order. The tested rule decides whether to place, hold, or close. An alert for a different account is recorded and not sent.

Webhook URL: `https://YOUR_PUBLIC_HOST/hooks/tradingview/YOUR_TOKEN`

For a Tradovate-linked CrossTrade account, add `destination=tradovate;`.

8:00am and 3:00pm re-read the strategy. They keep it when the new rating has the same side, and adjust it when the side changes.

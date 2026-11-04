This file is futuresfund/researchNotes/how_to_research.md.

Limit risk

- The session is 18:00 to 16:00 New York. Flat the trade when the session ends.
- Every trade has a dollar stop. One trade down more than $750, 1.5 percent of the account, is out, costs included. The stop stays at or under $700, because costs add up to $36 a trade. The take profit stays larger than the stop. There is no maximum on the take profit or the profit.
- The pro account rule: the drawdown has no limit until the profit reaches the allowed drawdown, $2,500 on a $50,000 account. From there it may not exceed 30 percent of the profit. Keep the drawdown low before that as well.
- The open loss closes the trade when it reaches stop_dollars. No ATR stop.
- A created script waits in draftStrategies while engine 4 develops it on the last year of the instrument and chart it was written for. It enters createdStrategies only after one run clears the acceptance. Only then does another chart use it.
- A draft that never has a run clear the acceptance moves to unprovenStrategies. It is kept, and it is not tested again.
- After 20 attempts with no profit, the draft is scrapped to unprovenStrategies and a new one is written.

Delta volume breakout first

- The golden strategy is a delta volume breakout: a volume profile over a lookback with up and down volume on every row, the point of control, the net delta at the point of control, and a breakout of that zone with the delta on the same side.
- The desk needs a delta volume breakout result that clears the acceptance on every instrument, on at least one of the 2-minute, 5-minute, and 15-minute charts. The golden strategy's results are the bar.
- Every chart studies the delta volume breakout scripts before any other script. On those scripts, an instrument that has no such result yet is tried first.
- Every new script the strategy developers write is a delta volume breakout for one instrument that has no such result yet. A script without the core is sent back.

The stop and the take profit

The stop holds at the entry price for the whole trade. It never moves with the close, and it is in place from the first bar. The desk writes this line into every script before a backtest.

The take profit stays on the last close on purpose. A script that resends its exit every bar moves the target ahead of price, and that lets a winner run past the take profit input. A win larger than the take profit is not a fault. Do not lock it to the entry price.

A stop can only fill at the next price that trades. When price jumps past the stop between bars, the trade loses more than the stop. That is the market and not a broken stop, so do not repair the script for it. The one-trade loss rule and the stop check leave out that jump. Profit and drawdown still count all of it.

One acceptance check

Every table, filing, email, and count reads pass or fail from objective.acceptance_misses. The Not tested table and the strategy library use the same check.

A script whose stop, take profit, or input does not work is never refused. It keeps running, the researcher repairs it, and its charts run again on the repaired script.

Research the script before tuning it

- Before the first backtest, every input is moved to both ends on the last month. An input that changes no trade is left out of the search, and the researcher is told.
- The researcher reads the script when it is first opened, when settings stop moving the result, and when a stage fails. No engine waits for it: the engines keep backtesting, and the answer is used the next time a chart opens that script.
- A script that ignores its own settings is broken. Repair it before changing a setting. An exit that uses another input for its stop or target should read stop_dollars and tp_dollars.
- A rewrite is measured at the same settings. It stays when it connects an input without costing profit, or when the result is better. Otherwise it is taken back.

Skip the time of day that keeps losing

- Every backtest keeps its closed trades by the half hour they entered, New York time.
- When a stage fails, find the stretch of the day, half an hour to two hours long, where the trades lost the most together. It must hold at least 4 trades and a quarter of the run's losses, from a run of at least 12 trades.
- Add a Skip Window section with use_skip, skip_start_hr, skip_start_mn, skip_end_hr, and skip_end_mn. No trade opens inside the window, and an open trade is closed when it starts. It applies on top of the session.
- The section is measured at the same settings like any rewrite, and it stays only when it holds. A script that already has the section only has its window moved. The next chart starts with it.
- Read the passed stages too. When the runs that passed the 7, 30, and 90 days and the year keep losing at the same time of day, such as the open when volume is high, skip that time. It counts only when at least two of those runs traded there and every one of them lost money there. The skip is measured on the year and kept only when the result is better.
- A script can also trade two sessions, such as 18:00 to 08:00 and 12:00 to 16:00, to leave out the open. Use a Session 2 section with its own hour and minute inputs.

Redesign a script that keeps losing

A setting or a repaired line cannot fix a script whose idea loses. After a failed stage, or when settings stop moving the result, the researcher may redesign it.

- A redesign can change the entries, the filters, the sessions, and the exits, add inputs, or replace the logic completely. Change whatever the script needs to make money consistently without losing more than the drawdown rule allows.
- It is saved as a new script in the learning folder. The original stays exactly as it was, and the charts take the new script next.
- It is not compared with the old result at the old settings. It goes through the stages from the start: 7 days, 30, 90, then the year.
- It keeps the risk rules: the dollar stop held at the entry price, the stop at or under $700, the take profit larger than the stop, and flat when the session ends. No ATR stop.
- An original can be redesigned 3 times. The researcher reads what was already tried and does not repeat it.

Prove in stages

- Every script is backtested on the last 7 days first, then the last 30 days, then the last 90 days, then the full 365 days. A stage counts back from the last bar, in futures sessions: 7 days is 5 sessions, 30 days is 21, 90 days is 64.
- A script does not move to the next stage until it passes the one it is on. A failed stage ends the study there. The longer backtests are not run.
- A stage passes only when the backtest is profitable and meets the acceptance criteria for its length:
  - Net profit above zero, and above the yearly profit target times days/365 when that box is on.
  - At least 3 trades a week, and at least the yearly trade minimum times days/365 when that box is on. 90 days at 100 trades a year needs 25.
  - No single trade down more than 1.5 percent of the account, and drawdown inside the pro account rule.
  - Profit factor and win rate above the yearly lines when those boxes are on.
- Each stage gets up to 20 attempts. Change one input at a time, as below. The settings that pass a stage are where the next stage starts. A stage that fails gets one more search with the losing time of day skipped.
- The 365 day stage is the full search. A script is filed in the library only when the year meets every acceptance criterion.
- This applies to every script the chart researchers study. Do not skip a stage because a script did well somewhere else.

Then raise profit

- Change one input at a time. Keep a step that improves the result. Reverse a step that adds risk, even if the profit rose.
- An input that leaves the result exactly as it was (the same profit, drawdown, trade count, and win rate) did nothing that way. The search does not move it that way again. A number that changed nothing up and down, or a switch or choice that changed nothing when flipped, is out of that search and stays where it was. The attempts go to the inputs that change the result. A repeat that taught this does not count toward leaving the script. The next, longer stage has more trades, so it tries every input again.
- What each input did is kept in researchNotes/input_effects, one file per script: how often it was moved, how often it changed the result, and how far profit moved. Use it to tell which inputs matter. A new script keeps the inputs that move results and leaves out the ones that never changed one.
- Read the description at the top of the script, the lines under "How this script works". The Trading Analyst wrote that description. Think about how a change affects the trade that description names, then change the indicator value that reduces risk or raises profit.
- Start at a $250 stop and a $500 take profit. Raise the stop through $700, then raise the take profit. It has no maximum. Keep the pair that makes the most profit inside that risk.
- If a week is under 2 trades, loosen the entry. If a year is over 600 trades, tighten the entry.
- Every script uses this style of exit.

```
stop_dollars = input.int(500, "Stop ($)", minval=250, maxval=700)
tp_dollars = input.int(1000, "Take Profit ($)", minval=250)
long_stop = strategy.position_size > 0 ? strategy.position_avg_price - stop_dollars / pv : close - stop_dollars / pv
long_limit = close + tp_dollars / pv
short_stop = strategy.position_size < 0 ? strategy.position_avg_price + stop_dollars / pv : close + stop_dollars / pv
short_limit = close - tp_dollars / pv
if strategy.position_size != 0 and not na(stop_dollars)
    if strategy.openprofit <= -stop_dollars
        strategy.close_all(comment="Open P&L > SL Risk")
```

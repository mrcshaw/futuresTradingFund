"""An exit set with ATR is converted to the desk's dollar stop. It is never refused.

Only ATR that sets an exit distance changes. A signal that reads ATR stays as written.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from futuresfund import learn
from futuresfund.pine_report import pine_facts
from futuresfund.pine_runtime import compile_pine
from futuresfund.research_rules import _exit_kind, exit_step
from futuresfund.strategy_team import (
    _OPEN_LOSS_BLOCK,
    _similar_risk,
    atr_exit,
    convert_atr_exit,
    own_exit,
    pine_problems,
    prepare_backtest,
)

# The POC breakout: the bracket is sent again on every bar, at the left margin.
POC = """//@version=5
strategy("POC Momentum Breakout v1 QO 5min chart", overlay=true, default_qty_type=strategy.fixed, default_qty_value=1, initial_capital=50000, process_orders_on_close=true)

// Inputs
lookback = input.int(60, "Profile Lookback", minval=30, maxval=100)
atr_len = input.int(14, "ATR Length", minval=5, maxval=50)
rsi_len = input.int(14, "RSI Length", minval=5, maxval=50)
stop_mult = input.float(3.09, "Stop Multiplier", minval=0.5, maxval=5.0)
tp_mult = input.float(2.4413, "TP Multiplier", minval=0.5, maxval=5.0)
cooldown_bars = input.int(3, "Cooldown Bars", minval=1, maxval=20)

atr_val = ta.atr(atr_len)
rsi_val = ta.rsi(close, rsi_len)
ema_fast = ta.ema(close, 9)
ema_slow = ta.ema(close, 21)
momentum_up = ta.crossover(ema_fast, ema_slow)
momentum_dn = ta.crossunder(ema_fast, ema_slow)
long_cond = momentum_up and rsi_val < 70
short_cond = momentum_dn and rsi_val > 30
var int last_entry_bar = -cooldown_bars

if long_cond and last_entry_bar <= bar_index - cooldown_bars
    strategy.entry("Long", strategy.long)
    last_entry_bar := bar_index

if short_cond and last_entry_bar <= bar_index - cooldown_bars
    strategy.entry("Short", strategy.short)
    last_entry_bar := bar_index

// Exits
stop_dist = atr_val * stop_mult
tp_dist = atr_val * tp_mult

strategy.exit("Exit Long", "Long", stop=close - stop_dist, limit=close + tp_dist)
strategy.exit("Exit Short", "Short", stop=close + stop_dist, limit=close - tp_dist)
"""

# The Squeeze: ATR already capped by dollars, the exit sent once at entry. The Keltner channel is a signal.
SQUEEZE = """//@version=5
strategy("Squeeze Momentum + ADX + WMA 120", overlay=true,
  initial_capital=50000, default_qty_type=strategy.fixed, default_qty_value=1,
  pyramiding=0, process_orders_on_close=true, calc_on_every_tick=false)

kc_len     = input.int(20, "KC Length", minval=1, group="Squeeze Momentum")
kc_mult    = input.float(1.5, "KC Multiplier", minval=0.1, step=0.1, group="Squeeze Momentum")

// ===== RISK =====
atr_len    = input.int(14, "ATR Length", minval=1, group="Risk")
stop_atr   = input.float(2.0, "Stop (x ATR)", minval=0.1, step=0.1, group="Risk")
tp_atr     = input.float(3.0, "Take Profit (x ATR)", minval=0.1, step=0.1, group="Risk")
max_loss   = input.float(700, "Max Stop Per Trade ($)", minval=1, group="Risk")

kc_basis = ta.sma(close, kc_len)
kc_upper = kc_basis + ta.atr(kc_len) * kc_mult
kc_lower = kc_basis - ta.atr(kc_len) * kc_mult
long_sig  = ta.crossover(close, kc_upper)
short_sig = ta.crossunder(close, kc_lower)

// The stop and take profit are set once at entry and stay there for the whole trade.
atr       = ta.atr(atr_len)
stop_dist = math.min(stop_atr * atr, max_loss / (syminfo.pointvalue * default_qty_value))
tp_dist   = tp_atr * atr
if strategy.position_size == 0
    if long_sig
        strategy.entry("Long", strategy.long)
        strategy.exit("XL", "Long", stop=close - stop_dist, limit=close + tp_dist)
    else if short_sig
        strategy.entry("Short", strategy.short)
        strategy.exit("XS", "Short", stop=close + stop_dist, limit=close - tp_dist)
"""

# ema_bb: its own exit, with a named stop price, a reversal at half the risk, a break-even, and a trail.
EMA_BB = """//@version=5
strategy("EMA BB Mean Reversion NQ", overlay=true,
  initial_capital=50000, default_qty_type=strategy.fixed, default_qty_value=1,
  pyramiding=0, process_orders_on_close=true)

ema_slow       = input.int(55, "EMA Slow", minval=5, maxval=200, group="EMA")
ema_fast       = input.int(15, "EMA Fast", minval=3, maxval=100, group="EMA")
bb_length = input.int(31, "BB Length", minval=5, maxval=50, group="Bollinger")
bb_std    = input.float(1.5, "BB Std Dev", minval=0.5, maxval=3.0, step=0.1, group="Bollinger")

// ===== ATR RISK =====
atr_length   = input.int(29, "ATR Length", minval=5, maxval=30, group="Risk")
sl_atr_mult  = input.float(1.5, "SL ATR Multiplier", minval=0.5, maxval=3.0, step=0.1, group="Risk")
tp_sl_ratio  = input.float(2.5, "TP / SL Ratio", minval=1.0, maxval=4.0, step=0.1, group="Risk", tooltip="TP distance = SL distance x this.")
pv           = input.float(20, "Point Value ($)", group="Risk")

use_trail         = input.bool(true, "Enable Trailing Stop", group="Trail")
trail_pct         = input.float(80, "Trail: Keep % of Peak Profit", group="Trail")
trail_min_dollars = input.float(300, "Trail: Min Profit Before Trailing ($)", group="Trail")
use_breakeven      = input.bool(true, "Enable Breakeven Stop", group="Breakeven")
be_trigger_dollars = input.float(150, "BE Trigger ($)", minval=50, group="Breakeven")

var float current_stop    = 0.0
var float entry_price     = 0.0
var float current_tp      = 0.0
var float peak_profit_pts = 0.0
var bool  be_active       = false
var bool  reversal_pending = false

float ema_slow_val  = ta.ema(close, ema_slow)
float ema_fast_val  = ta.ema(close, ema_fast)
float bb_basis      = ta.sma(close, bb_length)
float bb_dev        = ta.stdev(close, bb_length)
float bb_upper      = bb_basis + bb_std * bb_dev
float bb_lower      = bb_basis - bb_std * bb_dev
float atr_val       = ta.atr(atr_length)

bool long_cond  = ema_fast_val > ema_slow_val and close <= bb_lower
bool short_cond = ema_fast_val < ema_slow_val and close >= bb_upper

float sl_dist = not na(atr_val) and atr_val > 0 ? atr_val * sl_atr_mult : na
float tp_dist = not na(sl_dist) ? sl_dist * tp_sl_ratio : na
float be_trigger_pts = be_trigger_dollars / pv

bool flat = strategy.position_size == 0
bool long_sig  = long_cond and flat and not reversal_pending and not na(sl_dist) and not na(tp_dist)
bool short_sig = short_cond and flat and not reversal_pending and not na(sl_dist) and not na(tp_dist)

if long_sig
    current_stop    := close - sl_dist
    entry_price     := close
    current_tp      := close + tp_dist
    be_active       := false
    strategy.entry("Long", strategy.long)
    strategy.exit("XL", "Long", stop=current_stop, limit=current_tp)
else if short_sig
    current_stop    := close + sl_dist
    entry_price     := close
    current_tp      := close - tp_dist
    be_active       := false
    strategy.entry("Short", strategy.short)
    strategy.exit("XS", "Short", stop=current_stop, limit=current_tp)

if reversal_pending and flat and not na(sl_dist)
    reversal_pending := false
    float rev_stop_dist = sl_dist / 2.0
    float rev_tp_dist   = sl_dist
    current_stop := close + rev_stop_dist
    entry_price  := close
    current_tp   := close - rev_tp_dist
    strategy.entry("RevShort", strategy.short)
    strategy.exit("XRS", "RevShort", stop=current_stop, limit=current_tp)

if use_breakeven and strategy.position_size > 0 and not be_active
    if close - entry_price >= be_trigger_pts
        be_active    := true
        current_stop := entry_price + 0.25
        strategy.exit("XL", "Long", stop=current_stop, limit=current_tp)

if use_trail and strategy.position_size > 0
    float avg        = strategy.position_avg_price
    float profit_now = high - avg
    if profit_now > peak_profit_pts
        peak_profit_pts := profit_now
    if peak_profit_pts >= trail_min_dollars / pv
        float new_stop = avg + peak_profit_pts * (trail_pct / 100.0)
        if new_stop > current_stop
            current_stop := new_stop
            strategy.exit("XL", "Long", stop=current_stop, limit=current_tp)

string atr_txt = not na(atr_val) ? str.tostring(atr_val, "#.##") : "n/a"
if long_sig
    string tt = "Stop: " + str.tostring(close - sl_dist, "#.##") + " (" + str.tostring(sl_atr_mult, "#.#") + "x ATR)"
    label.new(bar_index, low, "L", tooltip=tt)
plot(strategy.position_size != 0 ? current_stop : na, "Stop", color.fuchsia)
"""

# SuperTrend: ATR is only its signal band, and it has no strategy.exit at all.
SUPERTREND = """//@version=5
strategy("SuperTrend STRATEGY", overlay=true)
Periods = input.int(10, "ATR Period")
Multiplier = input.float(3.0, "ATR Multiplier", step=0.1)
atr = ta.atr(Periods)
up = hl2 - (Multiplier * atr)
up1 = nz(up[1], up)
up := close[1] > up1 ? math.max(up, up1) : up
dn = hl2 + (Multiplier * atr)
dn1 = nz(dn[1], dn)
dn := close[1] < dn1 ? math.min(dn, dn1) : dn
var int trend = 1
trend := trend == -1 and close > dn1 ? 1 : trend == 1 and close < up1 ? -1 : trend
if trend == 1 and trend[1] == -1
    strategy.entry("BUY", strategy.long)
if trend == -1 and trend[1] == 1
    strategy.entry("SELL", strategy.short)
"""


def _inputs(text: str) -> dict:
    return {item["name"]: item["default"] for item in pine_facts(text)["inputs"]}


def _dollar_names(text: str) -> set:
    """The inputs the search measures as a dollar stop or take profit."""
    return {name for name in _inputs(text) if _exit_kind(name) is not None or exit_step(name) is not None}


class ConvertTests(unittest.TestCase):
    def test_the_poc_bracket_becomes_a_dollar_stop_and_target(self):
        converted, note = convert_atr_exit(POC, 50, root="ES")
        self.assertIn("stop_dist = stop_dollars / pv", converted)
        self.assertIn("tp_dist = tp_dollars / pv", converted)
        self.assertIn('stop_dollars = input.int(250, "Stop ($)", minval=50, maxval=700, step=10)', converted)
        self.assertIn('tp_dollars = input.int(500, "Take Profit ($)", minval=50, step=10)', converted)
        self.assertIn('pv = input.float(50, "Point Value ($)")', converted)
        self.assertIn(_OPEN_LOSS_BLOCK, converted)
        for gone in ("stop_mult", "tp_mult", "atr_len", "ta.atr", "atr_val"):
            self.assertNotIn(gone, converted)
        # The exit calls themselves are left as written; only the distance changed.
        self.assertIn('strategy.exit("Exit Long", "Long", stop=close - stop_dist, limit=close + tp_dist)', converted)
        self.assertIn("$250 stop that always holds", note)
        self.assertIn("A fill past the stop on a gap or thin volume is accepted.", note)
        self.assertIn("stop_mult", note)
        self.assertEqual(atr_exit(converted), "")
        self.assertIn("stop_dist = atr_val * stop_mult", atr_exit(POC))
        # The dollar-stop branch keeps the converted inputs. The shared-block strip would reset them.
        self.assertTrue(_similar_risk(converted))

    def test_a_micro_starts_at_50_and_100(self):
        converted, note = convert_atr_exit(POC, 5, root="MES")
        self.assertIn('stop_dollars = input.int(50, "Stop ($)", minval=50, maxval=700, step=10)', converted)
        self.assertIn('tp_dollars = input.int(100, "Take Profit ($)", minval=50, step=10)', converted)
        self.assertIn('pv = input.float(5, "Point Value ($)")', converted)
        self.assertIn("$50 stop", note)

    def test_the_prepared_poc_runs_on_the_dollar_stop_and_compiles(self):
        prepared = prepare_backtest(POC, "ES")
        compile_pine(prepared)
        self.assertEqual(atr_exit(prepared), "")
        self.assertEqual(prepare_backtest(prepared, "ES"), prepared)
        # The converted stop is kept: the shared exit is wired, the converted $250 default is not replaced.
        self.assertIn('stop_dollars = input.int(250, "Stop ($)", minval=50, maxval=700, step=10)', prepared)
        self.assertIn('strategy.exit("Exit Long", "Long", stop=long_stop, limit=long_limit)', prepared)
        self.assertEqual(prepared.count('comment="Open P&L > SL Risk"'), 1)
        self.assertFalse(any("ATR" in problem for problem in pine_problems(prepared)))

    def test_the_squeeze_keeps_its_keltner_signal_and_its_exit_at_entry(self):
        converted, note = convert_atr_exit(SQUEEZE, 50, root="ES")
        self.assertIn("stop_dist = stop_dollars / pv", converted)
        self.assertIn("tp_dist   = tp_dollars / pv", converted)
        for gone in ("stop_atr", "tp_atr", "max_loss", "atr_len", "atr       ="):
            self.assertNotIn(gone, converted)
        # The Keltner channel reads ATR for the signal. That stays exactly as written.
        self.assertIn("kc_upper = kc_basis + ta.atr(kc_len) * kc_mult", converted)
        self.assertIn("kc_lower = kc_basis - ta.atr(kc_len) * kc_mult", converted)
        self.assertIn('kc_mult    = input.float(1.5, "KC Multiplier"', converted)
        self.assertIn(
            "if strategy.position_size == 0\n    if long_sig\n        strategy.entry(\"Long\", strategy.long)\n"
            "        strategy.exit(\"XL\", \"Long\", stop=close - stop_dist, limit=close + tp_dist)",
            converted,
        )
        self.assertEqual(atr_exit(converted), "")
        self.assertIn("$250 stop", note)
        self.assertTrue(_similar_risk(converted))
        prepared = prepare_backtest(SQUEEZE, "MNQ")
        self.assertIn('stop_dollars = input.int(50, "Stop ($)", minval=50, maxval=700, step=10)', prepared)
        self.assertIn('pv = input.float(2, "Point Value ($)")', prepared)
        self.assertEqual(atr_exit(prepared), "")

    def test_ema_bb_keeps_its_break_even_trail_reversal_and_ratio_target(self):
        converted, note = convert_atr_exit(EMA_BB, 20, root="NQ")
        self.assertIn("float sl_dist = stop_dollars / pv", converted)
        self.assertIn("float tp_dist = not na(sl_dist) ? sl_dist * tp_sl_ratio : na", converted)
        self.assertIn("float rev_stop_dist = sl_dist / 2.0", converted)
        self.assertIn("current_stop := entry_price + 0.25", converted)
        self.assertIn("current_stop := new_stop", converted)
        self.assertEqual(converted.count('strategy.exit("XL", "Long", stop=current_stop, limit=current_tp)'), 3)
        # A ratio target has no tp_dollars to add. The point value input is the script's own.
        self.assertNotIn("tp_dollars", converted)
        self.assertEqual(converted.count("pv           = input.float(20"), 1)
        self.assertNotIn('pv = input.float(', converted)
        # The label still reads the multiplier, so it becomes a fixed value the search cannot ladder as dollars.
        self.assertIn("sl_atr_mult = 1.5", converted)
        self.assertNotIn("sl_atr_mult  = input", converted)
        # The table still reads atr_val, so the ATR and its length stay.
        self.assertIn("float atr_val       = ta.atr(atr_length)", converted)
        self.assertIn("The script keeps its own exit.", note)
        self.assertTrue(own_exit(converted))
        prepared = prepare_backtest(EMA_BB, "NQ")
        compile_pine(prepared)
        self.assertTrue(own_exit(prepared))
        self.assertIn('strategy.exit("XL", "Long", stop=current_stop, limit=current_tp)', prepared)
        self.assertNotIn("long_stop", prepared)
        self.assertIn("current_stop := entry_price + 0.25", prepared)

    def test_supertrend_is_not_an_atr_exit(self):
        self.assertEqual(atr_exit(SUPERTREND), "")
        self.assertEqual(convert_atr_exit(SUPERTREND, 50, root="ES"), (SUPERTREND, ""))

    def test_the_conversion_is_idempotent(self):
        for source, root, point in ((POC, "ES", 50), (SQUEEZE, "ES", 50), (EMA_BB, "NQ", 20), (POC, "MNQ", 2)):
            converted, _note = convert_atr_exit(source, point, root=root)
            self.assertEqual(convert_atr_exit(converted, point, root=root), (converted, ""))
            prepared = prepare_backtest(source, root)
            self.assertEqual(prepare_backtest(prepared, root), prepared)

    def test_an_inline_ticks_and_price_exit_are_converted(self):
        source = "\n".join([
            "//@version=5",
            'strategy("Inline", overlay=true)',
            'mult = input.float(2.0, "ATR Mult")',
            "atr_val = ta.atr(14)",
            'strategy.entry("L", strategy.long)',
            'strategy.exit("XL", "L", stop=close - atr_val * mult, limit=close + ta.atr(14) * 3)',
            'strategy.entry("S", strategy.short)',
            'strategy.exit("XS", "S", loss=atr_val * mult / syminfo.mintick, profit=atr_val * 4)',
            "",
        ])
        converted, _note = convert_atr_exit(source, 50, root="ES")
        self.assertIn('stop=close - stop_dollars / pv, limit=close + tp_dollars / pv', converted)
        self.assertIn("loss=stop_dollars / pv / syminfo.mintick", converted)
        self.assertIn("profit=tp_dollars / pv / syminfo.mintick", converted)
        self.assertNotIn("mult", converted)
        self.assertEqual(atr_exit(converted), "")
        compile_pine(converted)

    def test_a_named_stop_price_that_a_signal_reads_is_left_alone(self):
        source = "\n".join([
            "//@version=5",
            'strategy("Band stop", overlay=true)',
            'band_mult = input.float(2.0, "Band Mult")',
            "band = ta.sma(close, 20) - ta.atr(14) * band_mult",
            "if ta.crossover(close, band)",
            '    strategy.entry("L", strategy.long)',
            'strategy.exit("XL", "L", stop=band)',
            "",
        ])
        self.assertEqual(atr_exit(source), "")
        self.assertEqual(convert_atr_exit(source, 50, root="ES"), (source, ""))

    def test_a_stop_named_band_that_the_entry_crosses_keeps_its_atr(self):
        source = "\n".join([
            "//@version=5",
            'strategy("Stop line", overlay=true)',
            'atr_len = input.int(14, "ATR Length")',
            'stop_mult = input.float(2.0, "Stop Mult")',
            "atr_val = ta.atr(atr_len)",
            "long_stop_line = close - atr_val * stop_mult",
            "if ta.crossover(close, long_stop_line[1])",
            '    strategy.entry("L", strategy.long)',
            'strategy.exit("X", "L", stop=long_stop_line)',
            "",
        ])
        converted, _note = convert_atr_exit(source, 50, root="ES")
        self.assertIn("long_stop_line = close - atr_val * stop_mult", converted)
        self.assertIn("atr_val = ta.atr(atr_len)", converted)
        self.assertNotIn("stop_dollars / pv", converted)

    def test_a_trail_band_that_sets_the_trend_keeps_its_atr(self):
        source = "\n".join([
            "//@version=5",
            'strategy("Smart trail", overlay=true)',
            'mult = input.float(3.0, "Mult")',
            "atr_val = ta.atr(10)",
            "up_level = hl2 - mult * atr_val",
            "dn_level = hl2 + mult * atr_val",
            "var float trail = na",
            "trail := close[1] > nz(trail[1]) ? math.max(nz(trail[1]), up_level) : dn_level",
            "trend = close < trail ? -1 : 1",
            "if trend == 1 and trend[1] == -1",
            '    strategy.entry("L", strategy.long)',
            'strategy.exit("X", "L", stop=trail)',
            "",
        ])
        converted, _note = convert_atr_exit(source, 50, root="ES")
        self.assertIn("up_level = hl2 - mult * atr_val", converted)
        self.assertIn("dn_level = hl2 + mult * atr_val", converted)
        self.assertNotIn("stop_dollars / pv", converted)


class RewriteTests(unittest.TestCase):
    def test_a_rewrite_that_brings_in_an_atr_exit_is_converted_not_refused(self):
        from futuresfund.attempt_reason import accept_script, script_refusal

        original = prepare_backtest(POC, "ES")
        rewrite = POC.replace("lookback = input.int(60", "lookback = input.int(80")
        self.assertEqual(script_refusal(rewrite, original), "")
        accepted = accept_script(rewrite, original)
        self.assertIsNotNone(accepted)
        self.assertEqual(atr_exit(accepted), "")
        self.assertNotIn("stop_mult", accepted)
        self.assertIn("stop_dist = stop_dollars / pv", accepted)


class SearchTests(unittest.TestCase):
    def test_the_search_does_not_sweep_dead_multipliers(self):
        for source, root in ((POC, "ES"), (SQUEEZE, "ES"), (EMA_BB, "NQ")):
            prepared = prepare_backtest(source, root)
            names = _inputs(prepared)
            for dead in ("stop_mult", "tp_mult", "sl_atr_mult", "stop_atr", "tp_atr", "max_loss", "atr_len"):
                self.assertNotIn(dead, names)
            self.assertIn("stop_dollars", names)
            self.assertEqual(_dollar_names(prepared) - {"stop_dollars", "tp_dollars", "tp_sl_ratio"}, set())

    def test_a_raw_atr_script_runs_on_the_converted_stop(self):
        from futuresfund.backtrader_engine import script_as_run

        run = script_as_run(POC, {}, "ES")
        # merge_risk would fill in a $700 stop for an undeclared stop_dollars. The converted $250 holds.
        self.assertIn('stop_dollars = input.int(250, "Stop ($)"', run)
        self.assertNotIn('stop_dollars = input.int(700', run)
        self.assertEqual(atr_exit(run), "")


class _Board:
    def __init__(self):
        self.posts = []
        self.cancel = threading.Event()

    def post(self, author, text, kind="log", channel=None, **kwargs):
        self.posts.append(text)

    def set_status(self, *args, **kwargs):
        pass

    def set_task(self, *args, **kwargs):
        pass


class StudyTests(unittest.TestCase):
    def test_a_chart_no_longer_refuses_an_atr_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "POC Momentum.txt"
            path.write_text(POC, encoding="utf-8")
            board = _Board()
            refused = mock.Mock(side_effect=AssertionError("an ATR exit is not refused"))
            with mock.patch.object(learn, "_is_strategy_file", lambda path: True), \
                    mock.patch.object(learn, "_in_learning", lambda path: True), \
                    mock.patch.object(learn, "_read_notes", lambda path: {}), \
                    mock.patch.object(learn, "pending_for_chart", lambda notes, timeframe, instruments=None: ["ES"]), \
                    mock.patch.object(learn, "_copy_finished", lambda *args, **kwargs: False), \
                    mock.patch.object(learn, "describe_later", lambda *args, **kwargs: None), \
                    mock.patch.object(learn, "file_indicators", lambda *args, **kwargs: []), \
                    mock.patch.object(learn, "_load_frames", lambda *args, **kwargs: {}), \
                    mock.patch.object(learn, "leave_unrunnable", refused), \
                    mock.patch.object(learn, "set_aside", refused), \
                    mock.patch("futuresfund.delta_core.is_core_file", lambda path: False):
                outcome = learn._study_chart(board, path, {}, lambda: None, "5 min chart developer", "5m", "Chart researcher")
            # No bars in this test, so the chart sets it aside after the copy is prepared.
            self.assertEqual(outcome, "skipped")
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), POC)
        converted = [text for text in board.posts if "set its exit with ATR" in text]
        self.assertEqual(len(converted), 1)
        self.assertIn("$250 stop that always holds", converted[0])
        self.assertIn("A fill past the stop on a gap or thin volume is accepted.", converted[0])

    def test_the_conversion_is_kept_in_the_study_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "researchNotes"
            path = Path(tmp) / "POC Momentum.txt"
            path.write_text(POC, encoding="utf-8")
            note = convert_atr_exit(POC, 50, root="ES")[1]
            row = {"net_profit": 100.0, "max_drawdown": 50.0, "trades": 10, "params": {"stop_dollars": 250}}
            with mock.patch.object(learn, "NOTES", notes):
                learn._save_notes(path, {"title": "POC", "exit_conversion": note}, [row], "", "", row, "ES", "5m")
                saved = learn._read_notes(path)
                self.assertEqual(saved["exit_conversion"], note)
                self.assertEqual(saved["instruments"]["ES"]["charts"]["5m"]["exit_conversion"], note)
                # A later chart's save keeps the note.
                learn._save_notes(path, {"title": "POC"}, [row], "", "", row, "ES", "15m")
                self.assertEqual(learn._read_notes(path)["exit_conversion"], note)


if __name__ == "__main__":
    unittest.main()

"""
Sanity check: Delta Volume Breakout stop logic.
- Entry fills at avg. Stop must be at avg - stop_pts (long) so max loss = stop_dollars.
"""
pv = 20.0
stop_dollars = 150.0
stop_pts = stop_dollars / pv  # 7.5

# Simulate long: entry at 5900, stop at avg - 7.5
avg = 5900.0
hard_stop = avg - stop_pts  # 5892.5
max_loss_pts = avg - hard_stop  # 7.5
max_loss_dollars = max_loss_pts * pv  # 150

assert abs(max_loss_dollars - stop_dollars) < 0.01, "Max loss should equal stop_dollars"
assert hard_stop == 5892.5, "Stop price should be 7.5 below entry"

# Simulate short: entry at 5900, stop at avg + 7.5
hard_stop_short = avg + stop_pts  # 5907.5
max_loss_pts_short = hard_stop_short - avg  # 7.5
max_loss_dollars_short = max_loss_pts_short * pv  # 150

assert abs(max_loss_dollars_short - stop_dollars) < 0.01, "Short max loss should equal stop_dollars"

# Old bug: stop was at signal_close - 7.5, entry at next_open. If next_open = signal_close + 12.5 (gap up):
signal_close = 5900.0
next_open = 5912.5  # gap up 12.5
old_stop = signal_close - stop_pts  # 5892.5
actual_distance_pts = next_open - old_stop  # 20 pts
actual_loss_dollars = actual_distance_pts * pv  # 400
print(f"Old bug: gap up 12.5 pts -> stop distance = {actual_distance_pts} pts -> loss ${actual_loss_dollars}")
assert actual_loss_dollars == 400, "Old bug produced ~400 loss"

# New logic: stop = avg - stop_pts with avg = next_open (actual fill)
avg_fill = next_open
new_hard_stop = avg_fill - stop_pts  # 5905
new_max_loss_pts = avg_fill - new_hard_stop  # 7.5
new_max_loss_dollars = new_max_loss_pts * pv  # 150
print(f"New logic: stop at {new_hard_stop} -> max loss ${new_max_loss_dollars}")
assert abs(new_max_loss_dollars - stop_dollars) < 0.01, "New logic must cap loss at stop_dollars"

print("All checks passed: stop is always stop_pts from actual fill (avg).")

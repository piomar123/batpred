# Net-settlement-aware iBoost and car charging: review findings (parked)

Status: **parked**. The two commits before this note (`d0ac44a`, `7e72dff`) implement a first attempt. A four-way review (engine,
planner, tests/docs, adversarial) found design-level problems, so this branch is not to be merged as it is. This note records what
was built, what is wrong with it, and how a second attempt could fix it.

## Background

With `metric_net_settlement_window_minutes` set, import and export kWh cancel each other out within each settlement window. One
more kWh of load therefore costs the export rate in a window that nets to export (up to the window's spare export) and the import
rate otherwise. iBoost and car charging decide when to run with per-slot rate rules, not through the optimiser, so on the base
feature they still judge each slot by its own gross rates.

## What this branch does

1. The published plan (`save="best"`, Python engine) records, per window, how far the window nets to export with the
   controllable loads (iBoost and car charging) added back: `net_settlement_surplus` (`prediction.py`, copied to the base object
   in `plan.py`).
2. The next cycle turns the record into a per-window rate (`utils.net_settlement_window_info`, time-averaged import/export rates):
   - iBoost rate gates (thresholds, gas comparisons) use the export rate if the window has surplus, otherwise the import rate, in
     both engines (kernel ABI 9 / parity 16, `PkContext.iboost_gate_rate`, NaN = unknown).
   - Smart iBoost and car planning price each slot by blending its share of the surplus (export rate) with the rest (import
     rate), `utils.net_settlement_blended_prices`. Car candidates become one slot per plan interval.

What held up: Python and C++ agree bit for bit (2,320 extra fuzzed seeds with random gate records), the ctypes layout matches, all
six binaries report ABI 9 / parity 16, and with netting off or no record the results are identical to the base branch.

## Findings

### Bugs

1. **The recorded "surplus without controllable loads" is wrong whenever the load was not served by the grid.** The record adds
   the car and iBoost energy back onto the plan's grid flow. That is only right if the load would otherwise have been imported
   or exported. It is wrong when:
   - the battery covered the load (car charging from the battery, an iBoost plan slot covered by the battery);
   - the car is outside the CT clamp (`car_energy_reported_load: false`): its energy is never in `diff`, yet
     `car_load_energy_bypass` is added back (a window that nets to -1.0 kWh records +6.0);
   - iBoost diverts solar in the default (not excess-only) mode, which takes PV before the battery.

   The effect is lock-in: a window the car was planned in looks like an export window, so the next cycle prices it at the export
   rate and the car stays, even after it becomes dearer (reproduced on `predbat_debug_agile1`).
2. **The car can flip between two slots every cycle.** The record adds back the car's kWh but keeps the battery behaviour of the
   run that had the car in it. With `car_charging_from_battery: false` a planned car freezes battery discharge, the house load
   shows up as import, and the recorded surplus drops. Reproduced: the plan alternates 12:00 → 01:00 → 12:00 on every cycle;
   when 12:00 is the current slot the charger would switch on and off every 5 minutes. `iboost_prevent_discharge` has the same
   mechanism.
3. **Windows that net to import are priced at the window's time-averaged import rate**, not each slot's own rate. With a 40p and
   a 0p half-hour in one window, both cost 20p, so iBoost boosts in the 40p half-hour, and a 10p slot can be priced out of
   `car_charging_plan_max_price`. This hits hourly netting with 15 or 30 minute prices.
4. **Float noise decides the iBoost gate.** The gate tests `surplus > 0`; windows the battery balances record about 1e-17. In
   `predbat_debug_agile1`, 23 of 48 windows came out slightly positive, changing iBoost by up to 0.8 kWh.
5. **Each car and iBoost price against the whole surplus**, so the same spare export is planned several times (6.5 kWh planned
   against 4 kWh in one example).
6. **Two cars charging in one step are double-counted** (pre-existing in both engines: `load_yesterday += car_amount_premium`
   adds the running total). It now also corrupts the record.

### Risks and gaps

- Car candidates are whole plan-interval slots admitted by their start minute: a low-rate window starting at 01:15 becomes
  01:30-02:30 (15 minutes at the dear rate); a window ending mid-slot is charged for the whole slot.
- The current window's surplus includes metered car and iBoost energy, so a car that just started lowers its own window's
  surplus.
- A debug dump holds the record this cycle produced, not the one it planned with, so `--debug_file` replays with the wrong gates.
- `compare.py` re-plans compared tariffs with the live tariff's record (it resets the seed but not the record).
- On DST days the record is dropped across midnight for 120 and 1440 minute windows (harmless: plain rates for one cycle).
- Tests: no coverage for a car outside the CT clamp, for the import-side gates being unblocked, or for car slot alignment;
  `net_settlement_windows` fails instead of skipping when no kernel is available; the docs overstate "plain rates as before"
  (the car slot shape and candidate rule change too).

## Possible solutions for a second attempt

1. **Record the counterfactual, don't reconstruct it.** After the published best run, run the same plan once more (Python, no
   save) with the car slots and iBoost switched off and record that run's per-window net. The battery's reaction is then modelled
   instead of guessed, bypass/battery/diversion cases stop mattering, and the record no longer depends on the car and iBoost
   decisions it drives, which removes both lock-in and the flip-flop. Cost: one extra Python prediction per cycle.
2. **Price the import part at each slot's own rate.** Only the surplus share should use an export rate (the window's
   volume-weighted export rate, or the slot's own export rate).
3. **Give the gate a tolerance** (for example, treat |surplus| below 0.01 kWh as zero) and decide deliberately which side an exactly
   balanced window takes.
4. **Allocate the surplus in order**: plan each car, subtract the kWh it planned in each window, then plan the next car and then
   iBoost against what is left.
5. **Split car candidates at the edges of `low_rates` windows** (candidate = intersection of a plan slot with a low window, plus
   surplus slots), so behaviour without surplus matches the old path exactly.
6. **Fix the multi-car double count** in both engines (`load_yesterday += car_load_scale / car_charging_loss` per car), as its own
   change with its own parity revision.
7. Store the record the cycle planned with separately for debug replay; reset or ignore it in `compare.py`.
8. Tests: bypass car, battery-covered car, non-excess iBoost, import-side gates, multi-car, a multi-cycle stability test (plan,
   record, re-plan for several cycles, assert no flip-flop), and skip rather than fail without a kernel.

Solutions 1-3 are needed before this is worth merging; 4-8 make it robust.

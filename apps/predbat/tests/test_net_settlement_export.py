# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for exporting or charging against an imbalance already metered in the current net settlement window.

Under net settlement import and export kWh cancel each other out within a window. Once the window has
metered net import (a load spike beyond what the battery covers), exporting from the battery before it
ends cancels that import and is worth the import rate, however low the export rate is. Once it has
metered net export, charging from the grid cancels that export and only costs the export rate.
Plan.net_settlement_windows offers the rest of the window to the optimiser, Plan.net_settlement_replan_needed
recomputes the plan early so it can act (or stop) within the window, and the outputs label the windows.
"""

import copy
from datetime import timedelta

from const import EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE, EXPORT_MODE_TARGET
from utils import NetSettlementSeed, export_mode_of, pack_export_limit
from tests.test_infra import FIXTURE_MINUTES_NOW, reset_inverter, reset_rates
from tests.test_plan_why_reason import _setup_baseline, _flat_soc, _get_row, _codes

STATE_ATTRS = [
    "minutes_now",
    "midnight_utc",
    "rate_import",
    "rate_export",
    "plan_interval_minutes",
    "metric_net_settlement_window_minutes",
    "net_settlement_seed",
    "net_settlement_replan_last",
    "calculate_best_export",
    "set_export_window",
    "calculate_best_charge",
    "set_charge_window",
    "export_window_best",
    "export_limits_best",
    "charge_window_best",
    "charge_limit_best",
    "high_export_rates",
    "low_rates",
    "plan_valid",
    "soc_kw",
    "soc_max",
    "reserve",
    "cost_today_sofar",
    "rate_export_cost_threshold",
]


def snapshot(my_predbat):
    """Copy of the attributes these tests change"""
    return {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in STATE_ATTRS}


def restore(my_predbat, saved):
    """Put back what snapshot saved"""
    for attr, value in saved.items():
        setattr(my_predbat, attr, value)


def seed_for(minutes_now, window, import_kwh, export_kwh, import_rate=30.0, export_rate=1.0, window_offset=0):
    """A NetSettlementSeed for the current window (or one window_offset away) with the given metered totals"""
    return NetSettlementSeed(window=minutes_now // window + window_offset, import_kwh=import_kwh, import_cost=import_kwh * import_rate, export_kwh=export_kwh, export_credit=export_kwh * export_rate, applied=0.0)


def run_net_settlement_window_candidate_tests(my_predbat):
    """net_settlement_windows offers the rest of the window in the direction that cancels the metered imbalance, returns True on failure"""
    failed = False
    saved = snapshot(my_predbat)
    try:
        my_predbat.minutes_now = 12 * 60 + 25
        my_predbat.plan_interval_minutes = 30
        my_predbat.rate_import = {minute: (40.0 if minute < 12 * 60 + 30 else 20.0) for minute in range(0, 48 * 60)}
        my_predbat.rate_export = {minute: (3.0 if minute < 12 * 60 + 30 else 1.0) for minute in range(0, 48 * 60)}
        importing = seed_for(my_predbat.minutes_now, 60, 1.0, 0.2)
        exporting = seed_for(my_predbat.minutes_now, 60, 0.2, 1.0)

        # name, kind, window, seed, existing windows, expected [(start, end, average)]
        cases = [
            ("off", "export", 0, importing, [], []),
            ("no_seed", "export", 60, None, [], []),
            ("seed_other_window", "export", 60, seed_for(my_predbat.minutes_now, 60, 1.0, 0.2, window_offset=-1), [], []),
            ("export_when_net_export", "export", 60, exporting, [], []),
            ("charge_when_net_import", "charge", 60, importing, [], []),
            ("balanced_export", "export", 60, seed_for(my_predbat.minutes_now, 60, 0.5, 0.5), [], []),
            ("balanced_charge", "charge", 60, seed_for(my_predbat.minutes_now, 60, 0.5, 0.5), [], []),
            # 12:25 in the 12:00-13:00 window: the current slot and 12:30-13:00, export priced at the import rate it cancels
            ("export_rest_of_hour", "export", 60, importing, [], [(720, 750, 40.0), (750, 780, 20.0)]),
            # ... and charging priced at the export rate it cancels
            ("charge_rest_of_hour", "charge", 60, exporting, [], [(720, 750, 3.0), (750, 780, 1.0)]),
            ("overlap", "export", 60, importing, [{"start": 750, "end": 800, "average": 1.0}], [(720, 750, 40.0)]),
            ("all_covered", "charge", 60, exporting, [{"start": 700, "end": 800, "average": 1.0}], []),
            ("window_15", "export", 15, seed_for(my_predbat.minutes_now, 15, 1.0, 0.2), [], [(735, 750, 40.0)]),
            # A 90 minute window starting at 12:00 runs to 13:30; contiguous slots at the same rate are one window
            ("window_90", "export", 90, seed_for(my_predbat.minutes_now, 90, 1.0, 0.2), [], [(720, 750, 40.0), (750, 810, 20.0)]),
            ("list_seed", "export", 60, list(importing), [], [(720, 750, 40.0), (750, 780, 20.0)]),
        ]
        for name, kind, window, seed, existing, expected in cases:
            my_predbat.metric_net_settlement_window_minutes = window
            my_predbat.net_settlement_seed = seed
            windows = my_predbat.net_settlement_windows(kind, existing)
            got = [(item["start"], item["end"], item["average"]) for item in windows]
            if got != expected:
                print("ERROR: net_settlement_windows {}: got {} expected {}".format(name, got, expected))
                failed = True
            if any(not item.get("net_settlement") for item in windows):
                print("ERROR: net_settlement_windows {}: windows are not marked net_settlement".format(name))
                failed = True
    finally:
        restore(my_predbat, saved)
    return failed


def setup_trigger(my_predbat, window=60):
    """A mid-window plan state for the replan trigger: battery half full, 30p import, 1p export, no windows"""
    my_predbat.minutes_now = 12 * 60 + 25
    my_predbat.plan_interval_minutes = 30
    my_predbat.rate_import = {minute: 30.0 for minute in range(0, 48 * 60)}
    my_predbat.rate_export = {minute: 1.0 for minute in range(0, 48 * 60)}
    my_predbat.metric_net_settlement_window_minutes = window
    my_predbat.calculate_best_export = True
    my_predbat.set_export_window = True
    my_predbat.calculate_best_charge = True
    my_predbat.set_charge_window = True
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.reserve = 0.5
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.high_export_rates = []
    my_predbat.low_rates = []
    my_predbat.net_settlement_replan_last = None


def export_until(end, net=True):
    """An export window from 12:00 to end that really exports (target 0%)"""
    window = {"start": 720, "end": end, "average": 30.0}
    if net:
        window["net_settlement"] = True
    return window


def run_net_settlement_replan_tests(my_predbat):
    """net_settlement_replan_needed asks for a recompute only when it can help, returns True on failure"""
    failed = False
    saved = snapshot(my_predbat)

    def check(name, expected):
        """Evaluate the trigger twice (it must not change state) and compare"""
        nonlocal failed
        got = my_predbat.net_settlement_replan_needed()
        again = my_predbat.net_settlement_replan_needed()
        if got != expected or again != got:
            print("ERROR: net_settlement_replan_needed {}: got {} then {} expected {} (state {})".format(name, got, again, expected, my_predbat.net_settlement_replan_last))
            failed = True

    def seed(import_kwh, export_kwh):
        """Set the seed for the current window"""
        my_predbat.net_settlement_seed = seed_for(my_predbat.minutes_now, my_predbat.metric_net_settlement_window_minutes, import_kwh, export_kwh)

    try:
        setup_trigger(my_predbat)
        seed(0.05, 0.0)
        check("below_threshold", False)
        seed(0.3, 0.0)
        check("first_import", True)

        # A recompute that exported against it over the rest of the window: nothing more to do
        my_predbat.export_window_best = [export_until(780)]
        my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 0.0)]
        my_predbat.net_settlement_record_replan()
        state = my_predbat.net_settlement_replan_last
        if not state or state.get("kind") != "export" or not state.get("acted"):
            print("ERROR: net_settlement_record_replan after exporting recorded {}".format(state))
            failed = True
        seed(0.6, 0.0)
        check("covered_by_export", False)
        # The export only covered part of the window and the import has doubled: recompute
        my_predbat.export_window_best = [export_until(750)]
        check("partly_covered_and_doubled", True)
        seed(0.5, 0.0)
        check("partly_covered_not_doubled", False)
        # A freeze export window cannot cancel import, so it does not count as covering the window
        my_predbat.export_window_best = [export_until(780)]
        my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_FREEZE)]
        seed(0.6, 0.0)
        check("freeze_does_not_cover", True)
        # A rate window over the rest of the window: calculate_plan would offer nothing new there, so no recompute
        my_predbat.high_export_rates = [{"start": 720, "end": 780, "average": 1.0}]
        check("covered_by_rate_window", False)
        my_predbat.high_export_rates = []

        # A recompute that saw the windows and did not use them (declined, or the plan comparison kept the old
        # plan): ask again only once the imbalance has doubled, so the recomputes per window stay few
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []
        my_predbat.net_settlement_record_replan()
        if (my_predbat.net_settlement_replan_last or {}).get("acted"):
            print("ERROR: net_settlement_record_replan recorded acted with no export windows")
            failed = True
        seed(0.9, 0.0)
        check("declined_not_doubled", False)
        seed(1.2, 0.0)
        check("declined_doubled", True)

        # Guards: battery at reserve, a planned charge running now, import no dearer than export, export not allowed
        for name, attr, value in (
            ("at_reserve", "soc_kw", 0.5),
            ("export_rate_not_lower", "rate_export", None),
            ("charging_now", "charge_window_best", [{"start": 720, "end": 780, "average": 5.0}]),
            ("calculate_best_export_off", "calculate_best_export", False),
            ("set_export_window_off", "set_export_window", False),
        ):
            setup_trigger(my_predbat)
            if attr == "rate_export":
                my_predbat.rate_export = {minute: 30.0 for minute in range(0, 48 * 60)}
            else:
                setattr(my_predbat, attr, value)
            if attr == "charge_window_best":
                my_predbat.charge_limit_best = [9.0]
            seed(1.0, 0.0)
            check(name, False)

        # The charge direction mirrors it - charge windows are only offered alongside low rate windows
        setup_trigger(my_predbat)
        seed(0.0, 0.5)
        check("charge_no_low_rates", False)
        my_predbat.low_rates = [{"start": 1500, "end": 1560, "average": 10.0}]
        check("charge_first", True)
        my_predbat.soc_kw = my_predbat.soc_max
        check("charge_battery_full", False)

        # Stop once when the import a running net settlement export was cancelling has gone
        setup_trigger(my_predbat)
        seed(0.5, 0.0)
        my_predbat.export_window_best = [export_until(780)]
        my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 0.0)]
        my_predbat.net_settlement_record_replan()
        my_predbat.minutes_now = 12 * 60 + 40
        my_predbat.net_settlement_seed = seed_for(my_predbat.minutes_now, 60, 0.5, 0.5)
        check("cancelled_stop", True)
        # The recompute is recorded; even if the plan kept the running window it does not fire again
        my_predbat.net_settlement_record_replan()
        check("cancelled_stop_once", False)
        # An ordinary (not net settlement) export window running is left alone
        my_predbat.net_settlement_replan_last = None
        my_predbat.export_window_best = [export_until(780, net=False)]
        check("ordinary_export_not_stopped", False)

        # The state belongs to one absolute window: a daily window declined yesterday must not block today
        setup_trigger(my_predbat, window=1440)
        seed(1.0, 0.0)
        my_predbat.net_settlement_record_replan()
        check("daily_declined_today", False)
        my_predbat.midnight_utc = my_predbat.midnight_utc + timedelta(days=1)
        check("daily_next_day", True)
    finally:
        restore(my_predbat, saved)
    return failed


def run_plan(my_predbat, window, seed_import, seed_export, soc_kw, current_import_rate, low_rates=None):
    """calculate_plan with 1p export, 10p import after this hour and a given metered imbalance; returns (exporting, charging) windows"""
    reset_inverter(my_predbat)
    my_predbat.minutes_now = FIXTURE_MINUTES_NOW + 25
    reset_rates(my_predbat, 10.0, 1.0)
    for minute in range(FIXTURE_MINUTES_NOW, FIXTURE_MINUTES_NOW + 60):
        my_predbat.rate_import[minute] = current_import_rate
    my_predbat.args["threads"] = 0
    my_predbat.soc_kw = soc_kw
    my_predbat.cost_today_sofar = 0.0
    my_predbat.low_rates = low_rates or []
    # The 1p export rate is below the export threshold, so there are no ordinary export windows
    my_predbat.high_export_rates = []
    my_predbat.rate_export_cost_threshold = 5.0
    my_predbat.calculate_best_export = True
    my_predbat.set_export_window = True
    my_predbat.calculate_best_charge = True
    my_predbat.set_charge_window = True
    my_predbat.plan_valid = False
    my_predbat.net_settlement_replan_last = None
    my_predbat.metric_net_settlement_window_minutes = window
    my_predbat.net_settlement_seed = seed_for(my_predbat.minutes_now, 60, seed_import, seed_export) if (seed_import or seed_export) else None
    my_predbat.calculate_plan(recompute=True)
    exporting = [(item["start"], item["end"]) for item, limit in zip(my_predbat.export_window_best, my_predbat.export_limits_best) if export_mode_of(limit) != EXPORT_MODE_IDLE]
    charging = [(item["start"], item["end"]) for item, limit in zip(my_predbat.charge_window_best, my_predbat.charge_limit_best) if limit > my_predbat.reserve]
    return exporting, charging


def run_net_settlement_plan_tests(my_predbat):
    """calculate_plan exports against metered import and charges against metered export, returns True on failure"""
    failed = False
    saved = snapshot(my_predbat)
    saved_threads = my_predbat.args.get("threads")
    try:
        hour_end = FIXTURE_MINUTES_NOW + 60
        # Export: 30p import for the rest of this hour, 10p afterwards, so exporting now beats keeping the energy
        exporting, _ = run_plan(my_predbat, 60, 1.5, 0.0, 50.0, 30.0)
        if not exporting or any(end > hour_end for _, end in exporting) or not all(start < hour_end for start, _ in exporting):
            print("ERROR: with 1.5 kWh net import metered this hour the plan should export before 13:00, got {}".format(exporting))
            failed = True
        if my_predbat.high_export_rates:
            print("ERROR: net settlement windows leaked into high_export_rates: {}".format(my_predbat.high_export_rates))
            failed = True
        # The published export threshold must not become the 30p import rate the export windows cancel
        if my_predbat.rate_best_cost_threshold_export is not None and my_predbat.rate_best_cost_threshold_export > 1.0:
            print("ERROR: the export threshold was set from a net settlement window: {}".format(my_predbat.rate_best_cost_threshold_export))
            failed = True
        state = my_predbat.net_settlement_replan_last
        if not state or state.get("kind") != "export" or not state.get("acted"):
            print("ERROR: calculate_plan should record that it exported against the import, got {}".format(state))
            failed = True
        for label, window, seed_import in (("no_import", 60, 0.0), ("netting_off", 0, 1.5)):
            exporting, _ = run_plan(my_predbat, window, seed_import, 0.0, 50.0, 30.0)
            if exporting:
                print("ERROR: plan {} should not export at 1p, got {}".format(label, exporting))
                failed = True

        # Charge: 1.5 kWh net export metered this hour; charging now only cancels it at 1p, while the next low rate
        # window (overnight) is 10p
        night = [{"start": FIXTURE_MINUTES_NOW + 600, "end": FIXTURE_MINUTES_NOW + 660, "average": 10.0}]
        _, charging = run_plan(my_predbat, 60, 0.0, 1.5, 10.0, 30.0, low_rates=night)
        if not charging or not all(start < hour_end for start, _ in charging):
            print("ERROR: with 1.5 kWh net export metered this hour the plan should charge before 13:00, got {}".format(charging))
            failed = True
        if not any(window.get("net_settlement") for window in my_predbat.charge_window_best):
            print("ERROR: the charge window should be a net settlement window, got {}".format(my_predbat.charge_window_best))
            failed = True
        for label, window, seed_export in (("no_export", 60, 0.0), ("netting_off", 0, 1.5)):
            _, charging = run_plan(my_predbat, window, 0.0, seed_export, 10.0, 30.0, low_rates=night)
            if any(start < hour_end for start, _ in charging):
                print("ERROR: plan {} should not charge at 30p this hour, got {}".format(label, charging))
                failed = True
        # With no low rate windows at all the plan keeps the inverter's own charge windows, so none are added
        run_plan(my_predbat, 60, 0.0, 1.5, 10.0, 30.0)
        if any(window.get("net_settlement") for window in my_predbat.charge_window_best):
            print("ERROR: net settlement charge windows added without low rate windows: {}".format(my_predbat.charge_window_best))
            failed = True
    finally:
        restore(my_predbat, saved)
        if saved_threads is None:
            my_predbat.args.pop("threads", None)
        else:
            my_predbat.args["threads"] = saved_threads
    return failed


def run_net_settlement_output_tests(my_predbat):
    """Net settlement windows get their own why reason, published rates and no say in the published thresholds, returns True on failure"""
    failed = False
    pv_step, load_step = _setup_baseline(my_predbat)
    minutes_now = my_predbat.minutes_now
    my_predbat.manual_charge_times = []
    my_predbat.manual_freeze_charge_times = []
    my_predbat.manual_export_times = []
    my_predbat.manual_freeze_export_times = []
    my_predbat.manual_demand_times = []
    for minute in range(0, minutes_now + my_predbat.forecast_minutes + 60):
        my_predbat.rate_import[minute] = 30.0
        my_predbat.rate_export[minute] = 1.0

    def render():
        """The raw JSON plan"""
        return my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)[1]

    # Export against import
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = [{"start": minutes_now, "end": minutes_now + 30, "average": 30.0, "net_settlement": True}]
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 10.0)]
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)
    raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["export_net_settlement"] or row["reasons"][0]["params"].get("rate") != "30.00":
        print("ERROR: net settlement export reason unexpected: {}".format(row and row.get("reasons")))
        failed = True
    if "export_net_settlement" not in raw_plan["reason_templates"] or "charge_net_settlement" not in raw_plan["reason_templates"]:
        print("ERROR: net settlement reason templates are not published")
        failed = True
    # The published export window rate is the export rate, not the import rate the window cancels
    my_predbat.publish_export_limit(my_predbat.export_window_best, my_predbat.export_limits_best, best=True)
    rate = my_predbat.dashboard_values.get(my_predbat.prefix + ".best_export_start", {}).get("attributes", {}).get("rate")
    if rate != 1.0:
        print("ERROR: best_export_start rate for a net settlement window should be the 1p export rate, got {}".format(rate))
        failed = True

    # Charge against export
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.charge_window_best = [{"start": minutes_now, "end": minutes_now + 30, "average": 1.0, "net_settlement": True}]
    my_predbat.charge_limit_best = [8.0]
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 2.0)
    row = _get_row(render(), minutes_now)
    if row is None or _codes(row) != ["charge_net_settlement"] or row["reasons"][0]["params"].get("rate") != "1.00":
        print("ERROR: net settlement charge reason unexpected: {}".format(row and row.get("reasons")))
        failed = True
    # The published charge window rate is the import rate, not the export rate the window cancels
    my_predbat.publish_charge_limit(my_predbat.charge_limit_best, my_predbat.charge_window_best, best=True)
    rates = {name: item.get("attributes", {}).get("rate") for name, item in my_predbat.dashboard_values.items() if name.startswith(my_predbat.prefix + ".best_charge") and "rate" in item.get("attributes", {})}
    if not rates or any(rate != 30.0 for rate in rates.values()):
        print("ERROR: best_charge rate for a net settlement window should be the 30p import rate, got {}".format(rates))
        failed = True

    failed |= run_net_settlement_plumbing_tests(my_predbat)
    return failed


def run_net_settlement_plumbing_tests(my_predbat):
    """Net settlement windows stay apart from ordinary windows in merging, the horizon and the price levels, returns True on failure.

    Needs my_predbat.prediction (sort_window_by_price_combined reads it), so runs after _setup_baseline.
    """
    failed = False
    minutes_now = my_predbat.minutes_now
    my_predbat.manual_all_times = []
    my_predbat.all_active_keep = {}
    my_predbat.all_active_keep_max = {}
    target = pack_export_limit(EXPORT_MODE_TARGET, 10.0)

    # Merging: a net settlement window and an ordinary one next to it with the same limit stay separate
    net_charge = {"start": minutes_now, "end": minutes_now + 30, "average": 1.0, "net_settlement": True}
    ordinary_charge = {"start": minutes_now + 30, "end": minutes_now + 60, "average": 1.0}
    _, windows = my_predbat.discard_unused_charge_slots([8.0, 8.0], [dict(net_charge), dict(ordinary_charge)], my_predbat.reserve)
    if [bool(window.get("net_settlement")) for window in windows] != [True, False]:
        print("ERROR: discard_unused_charge_slots merged a net settlement window with an ordinary one: {}".format(windows))
        failed = True
    _, windows = my_predbat.discard_unused_charge_slots([8.0, 8.0], [dict(ordinary_charge, start=minutes_now, end=minutes_now + 30), dict(ordinary_charge)], my_predbat.reserve)
    if len(windows) != 1:
        print("ERROR: discard_unused_charge_slots should still merge two ordinary windows: {}".format(windows))
        failed = True
    net_export = {"start": minutes_now, "end": minutes_now + 30, "average": 30.0, "net_settlement": True}
    ordinary_export = {"start": minutes_now + 30, "end": minutes_now + 60, "average": 20.0}
    _, windows = my_predbat.discard_unused_export_slots([target, target], [dict(ordinary_export, start=minutes_now, end=minutes_now + 30), dict(net_export, start=minutes_now + 30, end=minutes_now + 60)])
    if [bool(window.get("net_settlement")) for window in windows] != [False, True]:
        print("ERROR: discard_unused_export_slots merged an ordinary window with a net settlement one: {}".format(windows))
        failed = True

    # The horizon is anchored on the next real low rate window, not a net settlement charge window
    # (a one hour planning horizon, so the anchor rather than the forecast length decides the end)
    real_charge = {"start": minutes_now + 600, "end": minutes_now + 660, "average": 5.0}
    saved_plan_hours = my_predbat.forecast_plan_hours
    my_predbat.forecast_plan_hours = 1
    try:
        with_net = my_predbat.record_length([dict(net_charge), dict(real_charge)], [8.0, 8.0], 10.0)
        without_net = my_predbat.record_length([dict(real_charge)], [8.0], 10.0)
    finally:
        my_predbat.forecast_plan_hours = saved_plan_hours
    if with_net != without_net:
        print("ERROR: record_length moved the horizon for a net settlement charge window: {} vs {}".format(with_net, without_net))
        failed = True

    # Price levels (and the published thresholds) ignore net settlement windows entirely
    def levels(charge_windows, charge_limits, export_windows, export_limits):
        """find_price_levels for these windows"""
        _, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_windows, export_windows)
        return my_predbat.find_price_levels(price_set, price_links, window_index, charge_limits, charge_windows, export_windows, export_limits)

    ordinary_exports = [{"start": minutes_now + 120, "end": minutes_now + 150, "average": 20.0}, {"start": minutes_now + 180, "end": minutes_now + 210, "average": 25.0}]
    cheap_net_export = {"start": minutes_now, "end": minutes_now + 30, "average": 10.0, "net_settlement": True}
    got = levels([], [], [cheap_net_export] + ordinary_exports, [target] * 3)
    expected = levels([], [], ordinary_exports, [target] * 2)
    if got != expected:
        print("ERROR: find_price_levels export side changed by a net settlement window: {} vs {}".format(got, expected))
        failed = True
    ordinary_charges = [{"start": minutes_now + 120, "end": minutes_now + 150, "average": 5.0}, {"start": minutes_now + 180, "end": minutes_now + 210, "average": 8.0}]
    dear_net_charge = {"start": minutes_now, "end": minutes_now + 30, "average": 12.0, "net_settlement": True}
    got = levels([dear_net_charge] + ordinary_charges, [8.0] * 3, [], [])
    expected = levels(ordinary_charges, [8.0] * 2, [], [])
    if got != expected:
        print("ERROR: find_price_levels charge side changed by a net settlement window: {} vs {}".format(got, expected))
        failed = True

    # remove_intersecting_windows keeps the mark when an active export clips a net settlement charge window
    from utils import remove_intersecting_windows

    _, clipped = remove_intersecting_windows([8.0], [dict(net_charge, end=minutes_now + 60)], [target], [{"start": minutes_now, "end": minutes_now + 15, "average": 20.0}])
    if len(clipped) != 1 or not clipped[0].get("net_settlement") or clipped[0]["start"] != minutes_now + 15:
        print("ERROR: remove_intersecting_windows lost the net settlement mark when clipping: {}".format(clipped))
        failed = True
    return failed


def run_net_settlement_update_pred_tests(my_predbat):
    """update_pred passes net_settlement_replan_needed into calculate_plan's recompute, returns True on failure"""
    failed = False

    class StopCycle(Exception):
        """Raised by the calculate_plan stub to end the cycle"""

    captured = []
    stubs = {
        "download_predbat_releases": lambda *args, **kwargs: None,
        "fetch_config_options": lambda *args, **kwargs: None,
        "fetch_sensor_data": lambda *args, **kwargs: False,
        "fetch_inverter_data": lambda *args, **kwargs: True,
        "dynamic_load": lambda *args, **kwargs: False,
        "net_settlement_replan_needed": lambda *args, **kwargs: replan_answer[0],
    }

    def fake_calculate_plan(recompute=True, debug_mode=False, publish=True):
        """Capture recompute and end the cycle"""
        captured.append(recompute)
        raise StopCycle()

    stubs["calculate_plan"] = fake_calculate_plan
    replan_answer = [False]
    saved = {name: my_predbat.__dict__.get(name) for name in stubs}
    saved_plan = {attr: getattr(my_predbat, attr) for attr in ("plan_valid", "plan_last_updated", "rate_min", "rate_max")}
    try:
        for name, stub in stubs.items():
            setattr(my_predbat, name, stub)
        my_predbat.rate_min = 5.0
        my_predbat.rate_max = 30.0
        for answer in (False, True):
            replan_answer[0] = answer
            # A valid, fresh plan, so nothing else asks for a recompute
            my_predbat.plan_valid = True
            my_predbat.update_time(print=False)
            my_predbat.plan_last_updated = my_predbat.now_utc
            try:
                my_predbat.update_pred(scheduled=True)
            except StopCycle:
                pass
        if captured != [False, True]:
            print("ERROR: update_pred should recompute exactly when net_settlement_replan_needed says so, calculate_plan got recompute {}".format(captured))
            failed = True
    finally:
        for name, value in saved.items():
            if value is None:
                my_predbat.__dict__.pop(name, None)
            else:
                setattr(my_predbat, name, value)
        for attr, value in saved_plan.items():
            setattr(my_predbat, attr, value)
    return failed


def run_net_settlement_export_tests(my_predbat):
    """Run the tests for acting on an imbalance metered in the current net settlement window, returns True on failure"""
    print("**** Running net settlement export and charge tests ****")
    failed = False
    failed |= run_net_settlement_window_candidate_tests(my_predbat)
    failed |= run_net_settlement_replan_tests(my_predbat)
    failed |= run_net_settlement_plan_tests(my_predbat)
    failed |= run_net_settlement_update_pred_tests(my_predbat)
    failed |= run_net_settlement_output_tests(my_predbat)
    if not failed:
        print("PASS")
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for exporting against import already metered in the current net settlement window.

Under net settlement import and export kWh cancel each other out within a window, so once a window has
metered net import (an unplanned kettle, a load spike beyond the battery's discharge rate), exporting
from the battery before the window ends is worth the import rate, however low the export rate is.
Plan.net_settlement_export_windows offers the rest of the window to the optimiser as export windows and
Plan.net_settlement_replan_needed recomputes the plan early so it can act within the window.
"""

import copy

from const import EXPORT_MODE_IDLE
from utils import NetSettlementSeed, export_mode_of
from tests.test_infra import FIXTURE_MINUTES_NOW, reset_inverter, reset_rates

EXPORT_ATTRS = [
    "minutes_now",
    "rate_import",
    "rate_export",
    "plan_interval_minutes",
    "metric_net_settlement_window_minutes",
    "net_settlement_seed",
    "net_settlement_replan_last",
    "calculate_best_export",
    "set_export_window",
    "export_window_best",
    "export_limits_best",
    "charge_window_best",
    "charge_limit_best",
    "high_export_rates",
    "low_rates",
    "plan_valid",
]


def seed_for(minutes_now, window, import_kwh, export_kwh, import_rate=30.0, export_rate=1.0, window_offset=0):
    """A NetSettlementSeed for the current window (or one window_offset away) with the given metered totals"""
    return NetSettlementSeed(
        window=minutes_now // window + window_offset,
        import_kwh=import_kwh,
        import_cost=import_kwh * import_rate,
        export_kwh=export_kwh,
        export_credit=export_kwh * export_rate,
        applied=0.0,
    )


def run_net_settlement_export_window_tests(my_predbat):
    """net_settlement_export_windows offers the rest of the window only when it has metered net import, returns True on failure"""
    failed = False
    saved = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in EXPORT_ATTRS}
    try:
        my_predbat.minutes_now = 12 * 60 + 25
        my_predbat.plan_interval_minutes = 30
        my_predbat.rate_import = {minute: (40.0 if minute < 12 * 60 + 30 else 20.0) for minute in range(0, 48 * 60)}
        my_predbat.rate_export = {minute: 1.0 for minute in range(0, 48 * 60)}
        importing = seed_for(my_predbat.minutes_now, 60, 1.0, 0.2)

        # name, window, seed, existing export windows, expected [(start, end, average)]
        cases = [
            ("off", 0, importing, [], []),
            ("no_seed", 60, None, [], []),
            ("seed_other_window", 60, seed_for(my_predbat.minutes_now, 60, 1.0, 0.2, window_offset=-1), [], []),
            ("nets_export", 60, seed_for(my_predbat.minutes_now, 60, 0.2, 1.0), [], []),
            ("balanced", 60, seed_for(my_predbat.minutes_now, 60, 0.5, 0.5), [], []),
            # 12:25 in the 12:00-13:00 window: the current slot 12:00-12:30 and 12:30-13:00, at the import rate
            ("rest_of_hour", 60, importing, [], [(720, 750, 40.0), (750, 780, 20.0)]),
            # A slot an existing export window already overlaps is left to it
            ("overlap", 60, importing, [{"start": 750, "end": 800, "average": 1.0}], [(720, 750, 40.0)]),
            ("all_covered", 60, importing, [{"start": 700, "end": 800, "average": 1.0}], []),
            # A 15 minute window shorter than the plan interval: just its own 15 minutes
            ("window_15", 15, seed_for(my_predbat.minutes_now, 15, 1.0, 0.2), [], [(735, 750, 40.0)]),
            # The seed as a plain list, as a debug dump replays it
            ("list_seed", 60, list(importing), [], [(720, 750, 40.0), (750, 780, 20.0)]),
        ]
        for name, window, seed, existing, expected in cases:
            my_predbat.metric_net_settlement_window_minutes = window
            my_predbat.net_settlement_seed = seed
            windows = my_predbat.net_settlement_export_windows(existing)
            got = [(item["start"], item["end"], item["average"]) for item in windows]
            if got != expected:
                print("ERROR: net_settlement_export_windows {}: got {} expected {}".format(name, got, expected))
                failed = True

        # A window aligned to the plan interval, part way through the hour's second slot
        my_predbat.minutes_now = 12 * 60 + 40
        my_predbat.metric_net_settlement_window_minutes = 60
        my_predbat.net_settlement_seed = seed_for(my_predbat.minutes_now, 60, 1.0, 0.2)
        got = [(item["start"], item["end"]) for item in my_predbat.net_settlement_export_windows([])]
        if got != [(750, 780)]:
            print("ERROR: net_settlement_export_windows at 12:40 got {} expected [(750, 780)]".format(got))
            failed = True
    finally:
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)
    return failed


def run_net_settlement_replan_tests(my_predbat):
    """net_settlement_replan_needed asks for a recompute once per 0.1 kWh of new net import in the window, returns True on failure"""
    failed = False
    saved = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in EXPORT_ATTRS}
    try:
        my_predbat.minutes_now = 12 * 60 + 25
        my_predbat.plan_interval_minutes = 30
        my_predbat.rate_import = {minute: 30.0 for minute in range(0, 48 * 60)}
        my_predbat.rate_export = {minute: 1.0 for minute in range(0, 48 * 60)}
        my_predbat.metric_net_settlement_window_minutes = 60
        my_predbat.calculate_best_export = True
        my_predbat.set_export_window = True
        my_predbat.export_window_best = []
        my_predbat.net_settlement_replan_last = None

        # (step name, minutes_now, import kWh, export kWh, existing export windows, expected)
        steps = [
            ("below_threshold", 12 * 60 + 25, 0.05, 0.0, [], False),
            ("first_import", 12 * 60 + 25, 0.3, 0.0, [], True),
            ("same_level", 12 * 60 + 30, 0.35, 0.0, [], False),
            ("grown", 12 * 60 + 35, 0.45, 0.0, [], True),
            ("export_window_in_plan", 12 * 60 + 40, 0.9, 0.0, [{"start": 720, "end": 780, "average": 1.0}], False),
            ("export_cancelled_some", 12 * 60 + 45, 0.9, 0.6, [], False),
            # A new window starts from scratch
            ("next_window", 13 * 60 + 5, 0.3, 0.0, [], True),
        ]
        for name, minutes_now, import_kwh, export_kwh, existing, expected in steps:
            my_predbat.minutes_now = minutes_now
            my_predbat.net_settlement_seed = seed_for(minutes_now, 60, import_kwh, export_kwh)
            my_predbat.export_window_best = existing
            got = my_predbat.net_settlement_replan_needed()
            if got != expected:
                print("ERROR: net_settlement_replan_needed step {}: got {} expected {} (last {})".format(name, got, expected, my_predbat.net_settlement_replan_last))
                failed = True

        # Never with netting off or when the plan cannot export
        for attr, value in (("metric_net_settlement_window_minutes", 0), ("calculate_best_export", False), ("set_export_window", False)):
            my_predbat.metric_net_settlement_window_minutes = 60
            my_predbat.calculate_best_export = True
            my_predbat.set_export_window = True
            my_predbat.net_settlement_replan_last = None
            my_predbat.export_window_best = []
            my_predbat.minutes_now = 12 * 60 + 25
            my_predbat.net_settlement_seed = seed_for(my_predbat.minutes_now, 60, 1.0, 0.0)
            setattr(my_predbat, attr, value)
            if my_predbat.net_settlement_replan_needed():
                print("ERROR: net_settlement_replan_needed asked for a recompute with {}={}".format(attr, value))
                failed = True
    finally:
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)
    return failed


def run_net_settlement_export_plan_tests(my_predbat):
    """The optimiser exports against metered import at a 1p export rate when netting is on, returns True on failure"""
    failed = False
    saved = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in EXPORT_ATTRS}
    saved_threads = my_predbat.args.get("threads")
    saved_misc = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in ("soc_kw", "cost_today_sofar", "rate_export_cost_threshold")}
    try:
        results = {}
        for label, window, seed_import in (("netting_import", 60, 1.5), ("netting_no_import", 60, 0.0), ("off", 0, 1.5)):
            reset_inverter(my_predbat)
            my_predbat.minutes_now = FIXTURE_MINUTES_NOW + 25
            reset_rates(my_predbat, 10.0, 1.0)
            # 30p import for the rest of this hour, 10p afterwards: energy kept in the battery is only worth 10p
            # later, while exporting it now cancels import metered at 30p
            for minute in range(FIXTURE_MINUTES_NOW, FIXTURE_MINUTES_NOW + 60):
                my_predbat.rate_import[minute] = 30.0
            my_predbat.args["threads"] = 0
            my_predbat.soc_kw = 50.0
            my_predbat.cost_today_sofar = 0.0
            my_predbat.low_rates = []
            # The 1p export rate is below the export threshold, so there are no ordinary export windows
            my_predbat.high_export_rates = []
            my_predbat.rate_export_cost_threshold = 5.0
            my_predbat.calculate_best_export = True
            my_predbat.set_export_window = True
            my_predbat.plan_valid = False
            my_predbat.metric_net_settlement_window_minutes = window
            my_predbat.net_settlement_seed = seed_for(my_predbat.minutes_now, 60, seed_import, 0.0) if seed_import else None
            my_predbat.calculate_plan(recompute=True)
            exporting = [(item["start"], item["end"]) for item, limit in zip(my_predbat.export_window_best, my_predbat.export_limits_best) if export_mode_of(limit) != EXPORT_MODE_IDLE]
            results[label] = exporting

        if not results["netting_import"] or any(end > FIXTURE_MINUTES_NOW + 60 for _, end in results["netting_import"]):
            print("ERROR: with 1.5 kWh net import metered this hour the plan should export before 13:00, got {}".format(results["netting_import"]))
            failed = True
        for label in ("netting_no_import", "off"):
            if results[label]:
                print("ERROR: plan {} should not export at 1p, got {}".format(label, results[label]))
                failed = True
    finally:
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)
        for attr, value in saved_misc.items():
            setattr(my_predbat, attr, value)
        if saved_threads is None:
            my_predbat.args.pop("threads", None)
        else:
            my_predbat.args["threads"] = saved_threads
    return failed


def run_net_settlement_export_tests(my_predbat):
    """Run the tests for exporting against import metered in the current net settlement window, returns True on failure"""
    print("**** Running net settlement export tests ****")
    failed = False
    failed |= run_net_settlement_export_window_tests(my_predbat)
    failed |= run_net_settlement_replan_tests(my_predbat)
    failed |= run_net_settlement_export_plan_tests(my_predbat)
    if not failed:
        print("PASS")
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for net-settlement-aware iBoost and car charging decisions.

Under net settlement, import and export kWh cancel each other out within a window, so one more kWh of
load costs the export rate in a window that nets to export and the import rate in one that nets to
import. The published plan records how far each window nets to export without the controllable loads
(iBoost and car charging); the next cycle uses that for the iBoost rate gates (both engines), the smart
iBoost plan and car slot planning. These tests cover each of those pieces.
"""

import copy
from datetime import timedelta

from const import PV_SCENARIO_NOMINAL
from prediction import Prediction
from prediction_kernel import create_kernel_context, run_prediction_kernel
from utils import net_settlement_surplus_record, net_settlement_window_info, net_settlement_effective_rate, net_settlement_blended_prices
from tests.test_infra import FIXTURE_MINUTES_NOW
from tests.test_kernel_parity import reset_net_settlement_base, make_mixed_step_data, snapshot_scenario_state, restore_scenario_state, compare_results, RESULT_NAMES

FINAL_IBOOST = RESULT_NAMES.index("final_iboost")


def run_net_settlement_helper_tests(my_predbat):
    """net_settlement_window_info / effective rate / blended prices, returns True on failure"""
    failed = False
    midnight = my_predbat.midnight_utc
    # Rates from yesterday to two days ahead, so re-keyed windows still find them
    rate_import = {minute: (30.0 if (minute // 30) % 2 == 0 else 20.0) for minute in range(-24 * 60, 3 * 24 * 60)}
    rate_export = {minute: 5.0 for minute in range(-24 * 60, 3 * 24 * 60)}
    record = net_settlement_surplus_record({10: 2.0, 11: -1.0, 12: 0.0}, 60, midnight)

    info = net_settlement_window_info(record, 60, midnight, rate_import, rate_export)
    if set(info) != {10, 11, 12} or info[10] != (2.0, 25.0, 5.0) or info[11][0] != -1.0:
        print("ERROR: net_settlement_window_info same day gave {}".format(info))
        failed = True

    # After midnight the same windows are 24 hours earlier on the new day's clock
    info_next = net_settlement_window_info(record, 60, midnight + timedelta(days=1), rate_import, rate_export)
    if set(info_next) != {-14, -13, -12}:
        print("ERROR: net_settlement_window_info next day ids {}".format(sorted(info_next)))
        failed = True
    # Windows with no rates are dropped
    if net_settlement_window_info(record, 60, midnight - timedelta(days=3), rate_import, rate_export):
        print("ERROR: net_settlement_window_info kept windows with no rates")
        failed = True

    for bad_record, window, why in ((None, 60, "no record"), (record, 30, "different window length"), (record, 0, "netting off")):
        if net_settlement_window_info(bad_record, window, midnight, rate_import, rate_export):
            print("ERROR: net_settlement_window_info should be empty for {}".format(why))
            failed = True
    # A record whose midnight does not land on a window boundary is not re-keyed
    if net_settlement_window_info(record, 60, midnight + timedelta(minutes=30), rate_import, rate_export):
        print("ERROR: net_settlement_window_info re-keyed a record across a misaligned midnight")
        failed = True

    if net_settlement_effective_rate((2.0, 25.0, 5.0)) != 5.0 or net_settlement_effective_rate((-1.0, 25.0, 5.0)) != 25.0 or net_settlement_effective_rate((0.0, 25.0, 5.0)) != 25.0:
        print("ERROR: net_settlement_effective_rate picked the wrong side")
        failed = True

    # 30 minutes inside a 60 minute window with 2 kWh surplus: this half gets 1 kWh of it
    info_flat = {10: (2.0, 30.0, 5.0)}
    price_import, price_export = net_settlement_blended_prices(info_flat, 60, 600, 630, 3.5, rate_import, rate_export)
    expected = (1.0 * 5.0 + 2.5 * 30.0) / 3.5
    if abs(price_import - expected) > 1e-9 or abs(price_export - expected) > 1e-9:
        print("ERROR: blended price {} {} expected {}".format(price_import, price_export, expected))
        failed = True
    # Load within the surplus costs the export rate only
    price_import, _ = net_settlement_blended_prices(info_flat, 60, 600, 630, 0.5, rate_import, rate_export)
    if abs(price_import - 5.0) > 1e-9:
        print("ERROR: blended price within surplus {} expected 5.0".format(price_import))
        failed = True
    # A window that nets to import costs the import rate
    price_import, _ = net_settlement_blended_prices({10: (-2.0, 30.0, 5.0)}, 60, 600, 630, 3.5, rate_import, rate_export)
    if abs(price_import - 30.0) > 1e-9:
        print("ERROR: blended price in an import window {} expected 30.0".format(price_import))
        failed = True
    # Unknown windows keep the plain per-step rates on each side
    price_import, price_export = net_settlement_blended_prices({}, 60, 600, 660, 3.5, rate_import, rate_export)
    if abs(price_import - 25.0) > 1e-9 or abs(price_export - 5.0) > 1e-9:
        print("ERROR: blended price with no window info {} {} expected 25.0 5.0".format(price_import, price_export))
        failed = True
    return failed


def setup_pinned_iboost(my_predbat, minutes_now):
    """Pinned lossless battery (grid flow = load - pv) with an iBoost diverting solar excess, rates 30p import / 5p export"""
    reset_net_settlement_base(my_predbat, minutes_now, import_rate=30.0, export_rate=5.0)
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 10.0
    my_predbat.reserve = 10.0
    my_predbat.best_soc_min = 0.0
    my_predbat.inverter_loss = 1.0
    my_predbat.inverter_limit = 20 / 60.0
    my_predbat.export_limit = 20 / 60.0
    my_predbat.iboost_enable = True
    my_predbat.iboost_solar = True
    my_predbat.iboost_solar_excess = True
    my_predbat.iboost_charging = False
    my_predbat.iboost_gas = False
    my_predbat.iboost_gas_export = False
    my_predbat.iboost_prevent_discharge = False
    my_predbat.iboost_on_export = True
    my_predbat.iboost_max_energy = 100.0
    my_predbat.iboost_max_power = 1.5 / 60.0
    my_predbat.iboost_min_power = 0.0
    my_predbat.iboost_min_soc = 0
    my_predbat.iboost_rate_threshold = 9999
    my_predbat.iboost_rate_threshold_export = 9999
    my_predbat.iboost_gas_scale = 1.0
    my_predbat.iboost_today = 0.0
    my_predbat.iboost_plan = []
    my_predbat.metric_net_settlement_window_minutes = 60


def run_both_engines(name, my_predbat, steps):
    """Run the Python engine and the kernel on the same inputs; returns (python_result, failed)"""
    pv_step, pv10_step, load_step, load10_step, pv90_step, load90_step = steps
    my_predbat.prediction_kernel_enable = False
    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step, pv90_step, load90_step)
    python_result = prediction.run_prediction([], [], [], [], PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes, save=None, cache=False)
    prediction.kernel_handle = create_kernel_context(prediction)
    kernel_result = run_prediction_kernel(prediction, [], [], [], [], PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes, 5, False) if prediction.kernel_handle else None
    if kernel_result is None:
        print("ERROR: {}: kernel run failed".format(name))
        return python_result, True
    return python_result, compare_results(name, python_result, kernel_result)


def run_net_settlement_surplus_recording_tests(my_predbat):
    """The published plan records pv - base load per window whatever iBoost and the car do, returns True on failure"""
    failed = False
    state = snapshot_scenario_state(my_predbat)
    had_prediction = "prediction" in my_predbat.__dict__
    saved_prediction = my_predbat.__dict__.get("prediction")
    saved_surplus = my_predbat.net_settlement_surplus
    try:
        minutes_now = FIXTURE_MINUTES_NOW + 25
        for variant in ("plain", "iboost", "car", "seeded"):
            setup_pinned_iboost(my_predbat, minutes_now)
            my_predbat.iboost_enable = variant == "iboost"
            if variant == "car":
                my_predbat.num_cars = 1
                my_predbat.car_energy_reported_load = True
                my_predbat.car_charging_slots = [[{"start": minutes_now + 60, "end": minutes_now + 240, "kwh": 21.0, "average": 30, "octopus": False}], [], [], []]
                my_predbat.car_charging_soc = [0, 0, 0, 0]
                my_predbat.car_charging_limit = [100, 100, 100, 100]
                my_predbat.car_charging_loss = 1.0
            seed_net = 0.0
            if variant == "seeded":
                my_predbat.net_settlement_seed = (minutes_now // 60, 0.7, 21.0, 0.2, 1.0, 14.0)
                seed_net = 0.2 - 0.7
            steps = make_mixed_step_data(my_predbat, pv_kw=3.0, load_kw=1.0)
            pv_step, _, load_step, _, _, _ = steps
            expected = {}
            for minute in range(0, my_predbat.forecast_minutes, 5):
                window_id = (minute + minutes_now) // 60
                expected[window_id] = expected.get(window_id, 0.0) + pv_step[minute] - load_step[minute]
            expected[minutes_now // 60] += seed_net

            my_predbat.prediction_kernel_enable = False
            my_predbat.prediction = Prediction(my_predbat, *steps)
            my_predbat.net_settlement_surplus = None
            result = my_predbat.run_prediction([], [], [], [], PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes, save="best")
            record = my_predbat.net_settlement_surplus
            if not record or record.get("window") != 60:
                print("ERROR: surplus recording {}: best run left record {}".format(variant, record))
                failed = True
                continue
            surplus = record["surplus"]
            if set(surplus) != set(expected) or any(abs(surplus[window_id] - value) > 1e-6 for window_id, value in expected.items()):
                bad = {window_id: (surplus.get(window_id), value) for window_id, value in expected.items() if abs(surplus.get(window_id, 1e9) - value) > 1e-6}
                print("ERROR: surplus recording {}: mismatched windows (recorded, expected) {}".format(variant, dict(list(bad.items())[:5])))
                failed = True
            if variant == "iboost" and not result[FINAL_IBOOST] > 0:
                print("ERROR: surplus recording iboost variant did not divert anything")
                failed = True

        # Only the published plan records - other saves and netting off leave nothing
        setup_pinned_iboost(my_predbat, minutes_now)
        steps = make_mixed_step_data(my_predbat, pv_kw=3.0, load_kw=1.0)
        prediction = Prediction(my_predbat, *steps)
        prediction.run_prediction([], [], [], [], PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes, save="compare")
        if prediction.net_settlement_surplus_best is not None:
            print("ERROR: a save=compare run recorded a surplus")
            failed = True
        my_predbat.metric_net_settlement_window_minutes = 0
        prediction = Prediction(my_predbat, *steps)
        prediction.run_prediction([], [], [], [], PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes, save="best")
        if prediction.net_settlement_surplus_best is not None:
            print("ERROR: a best run with netting off recorded a surplus")
            failed = True
    finally:
        restore_scenario_state(my_predbat, state)
        if had_prediction:
            my_predbat.prediction = saved_prediction
        else:
            my_predbat.__dict__.pop("prediction", None)
        my_predbat.net_settlement_surplus = saved_surplus
    return failed


def run_net_settlement_iboost_gate_tests(my_predbat):
    """iBoost rate gates use the window's effective rate in both engines, returns True on failure"""
    failed = False
    state = snapshot_scenario_state(my_predbat)
    try:
        minutes_now = FIXTURE_MINUTES_NOW + 25
        first = minutes_now // 60
        last = (minutes_now + my_predbat.forecast_minutes) // 60
        # Gas at 7p: export (5p) is below it, import (30p) is above it
        for gate in ("gas_export", "threshold_export"):
            results = {}
            for label in ("off", "no_record", "all_import", "all_export", "mixed"):
                setup_pinned_iboost(my_predbat, minutes_now)
                if gate == "gas_export":
                    my_predbat.iboost_gas_export = True
                    my_predbat.rate_gas = {minute: 7.0 for minute in range(0, minutes_now + my_predbat.forecast_minutes + 60)}
                else:
                    my_predbat.iboost_rate_threshold_export = 10.0
                if label == "off":
                    my_predbat.metric_net_settlement_window_minutes = 0
                surplus = {}
                if label == "all_import":
                    surplus = {window_id: -1.0 for window_id in range(first, last + 1)}
                elif label == "all_export":
                    surplus = {window_id: 1.0 for window_id in range(first, last + 1)}
                elif label == "mixed":
                    # Alternate import/export windows with every third window unknown
                    surplus = {window_id: (1.0 if window_id % 2 else -1.0) for window_id in range(first, last + 1) if window_id % 3}
                my_predbat.net_settlement_surplus = net_settlement_surplus_record(surplus, 60, my_predbat.midnight_utc) if label != "no_record" else None
                steps = make_mixed_step_data(my_predbat, pv_kw=3.0, load_kw=1.0)
                result, parity_failed = run_both_engines("iboost_gate_{}_{}".format(gate, label), my_predbat, steps)
                failed |= parity_failed
                results[label] = result[FINAL_IBOOST]
            # Plain rates: 5p export passes the gate. A window that nets to import prices the diverted
            # solar at the 30p import rate, so the gate blocks it; one that nets to export does not.
            if not results["off"] > 0 or results["no_record"] != results["off"] or results["all_export"] != results["off"]:
                print("ERROR: iBoost gate {}: expected the plain-rate diversion without an import record, got {}".format(gate, results))
                failed = True
            if results["all_import"] != 0:
                print("ERROR: iBoost gate {}: diverted {} kWh in windows netting to import".format(gate, results["all_import"]))
                failed = True
            if not 0 < results["mixed"] < results["off"]:
                print("ERROR: iBoost gate {}: mixed windows should divert some but not all, got {}".format(gate, results))
                failed = True
    finally:
        restore_scenario_state(my_predbat, state)
    return failed


def setup_rates(my_predbat, import_rate, export_rate, cheap_start, cheap_end, cheap_rate):
    """Flat import/export rates over the whole horizon with one cheaper import period"""
    horizon = my_predbat.minutes_now + my_predbat.forecast_minutes + 120
    my_predbat.rate_import = {minute: (cheap_rate if cheap_start <= minute < cheap_end else import_rate) for minute in range(0, horizon)}
    my_predbat.rate_export = {minute: export_rate for minute in range(0, horizon)}
    my_predbat.rate_min = cheap_rate


PLANNER_ATTRS = [
    "minutes_now",
    "rate_import",
    "rate_export",
    "rate_min",
    "rate_gas",
    "rate_import_cost_threshold",
    "plan_interval_minutes",
    "metric_net_settlement_window_minutes",
    "net_settlement_surplus",
    "iboost_enable",
    "iboost_smart",
    "iboost_today",
    "iboost_max_energy",
    "iboost_max_power",
    "iboost_smart_min_length",
    "iboost_rate_threshold",
    "iboost_rate_threshold_export",
    "iboost_gas",
    "iboost_gas_export",
    "num_cars",
    "car_charging_soc",
    "car_charging_limit",
    "car_charging_rate",
    "car_charging_loss",
    "car_charging_now",
    "car_charging_plan_smart",
    "car_charging_plan_max_price",
    "car_charging_plan_time",
]


def run_net_settlement_iboost_smart_tests(my_predbat):
    """Smart iBoost prefers a slot in a window with surplus over a cheaper-import slot, returns True on failure"""
    failed = False
    saved = {attr: copy.deepcopy(getattr(my_predbat, attr, None)) for attr in PLANNER_ATTRS}
    saved_log = my_predbat.__dict__.get("log")
    my_predbat.log = lambda *args, **kwargs: None
    try:
        my_predbat.minutes_now = 10 * 60
        my_predbat.plan_interval_minutes = 30
        # 15p import overnight (01:00-02:00 tomorrow), 30p otherwise, 5p export
        setup_rates(my_predbat, 30.0, 5.0, 25 * 60, 26 * 60, 15.0)
        my_predbat.rate_gas = {}
        my_predbat.iboost_enable = True
        my_predbat.iboost_smart = True
        my_predbat.iboost_today = 0.0
        my_predbat.iboost_max_energy = 1.5
        my_predbat.iboost_max_power = 3.0 / 60.0
        my_predbat.iboost_smart_min_length = 30
        # Only boost below 20p, so the plain 30p slots never qualify
        my_predbat.iboost_rate_threshold = 20
        my_predbat.iboost_rate_threshold_export = 9999
        my_predbat.iboost_gas = False
        my_predbat.iboost_gas_export = False
        # 12:00-13:00 today nets 3 kWh to export without the controllable loads
        my_predbat.net_settlement_surplus = net_settlement_surplus_record({12: 3.0, 13: -2.0}, 60, my_predbat.midnight_utc)

        plans = {}
        for label, window in (("off", 0), ("on", 60)):
            my_predbat.metric_net_settlement_window_minutes = window
            plans[label] = my_predbat.plan_iboost_smart()
        # Each day fills up to iboost_max_energy: without netting only tomorrow's 15p slot qualifies
        if [slot["start"] for slot in plans["off"]] != [25 * 60]:
            print("ERROR: smart iBoost without netting should only boost in the 15p slot, got {}".format(plans["off"]))
            failed = True
        # With netting today's 12:00 surplus costs 5p and qualifies; 13:00 nets to import at 30p and does not
        today = [slot for slot in plans["on"] if slot["start"] < 24 * 60]
        if len(today) != 1 or not (12 * 60 <= today[0]["start"] < 13 * 60) or abs(today[0]["average"] - 5.0) > 1e-9:
            print("ERROR: smart iBoost with netting should boost on the 12:00 surplus at 5p, got {}".format(plans["on"]))
            failed = True
        if [slot["start"] for slot in plans["on"] if slot["start"] >= 24 * 60] != [25 * 60]:
            print("ERROR: smart iBoost with netting should still use tomorrow's 15p slot, got {}".format(plans["on"]))
            failed = True
    finally:
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)
        if saved_log is None:
            del my_predbat.log
        else:
            my_predbat.log = saved_log
    return failed


def run_net_settlement_car_tests(my_predbat):
    """Car charging uses a window's surplus when it is the cheapest place to charge, returns True on failure"""
    failed = False
    saved = {attr: copy.deepcopy(getattr(my_predbat, attr, None)) for attr in PLANNER_ATTRS}
    saved_log = my_predbat.__dict__.get("log")
    my_predbat.log = lambda *args, **kwargs: None
    try:
        my_predbat.minutes_now = 10 * 60
        my_predbat.plan_interval_minutes = 30
        setup_rates(my_predbat, 30.0, 5.0, 25 * 60, 26 * 60, 15.0)
        my_predbat.rate_import_cost_threshold = 16.0
        low_rates = [{"start": 25 * 60, "end": 26 * 60, "average": 15.0}]
        my_predbat.num_cars = 1
        my_predbat.car_charging_soc = [0.0]
        my_predbat.car_charging_limit = [3.5]
        my_predbat.car_charging_rate = [7.0]
        my_predbat.car_charging_loss = 1.0
        my_predbat.car_charging_now = [False]
        my_predbat.car_charging_plan_max_price = [0]
        my_predbat.car_charging_plan_time = ["07:00:00"]

        cases = [
            # label, window, surplus record, smart, expected start of the single 3.5 kWh slot
            ("off", 0, {12: 8.0}, True, 25 * 60),
            ("no_surplus", 60, {12: -1.0}, True, 25 * 60),
            # 8 kWh surplus: a 30 minute slot gets 4 kWh of it, more than the 3.5 kWh the car needs -> 5p
            ("surplus", 60, {12: 8.0}, True, 12 * 60),
            # 4 kWh surplus: 2 kWh at 5p + 1.5 kWh at 30p is 15.7p, dearer than the 15p night slot
            ("small_surplus", 60, {12: 4.0}, True, 25 * 60),
            # Not smart: candidates in time order, the surplus slot comes first
            ("surplus_not_smart", 60, {12: 8.0}, False, 12 * 60),
        ]
        for label, window, surplus, smart, expected_start in cases:
            my_predbat.metric_net_settlement_window_minutes = window
            my_predbat.net_settlement_surplus = net_settlement_surplus_record(surplus, 60, my_predbat.midnight_utc)
            my_predbat.car_charging_plan_smart = [smart]
            plan = my_predbat.plan_car_charging(0, low_rates)
            if len(plan) != 1 or plan[0]["start"] != expected_start or abs(plan[0]["kwh"] - 3.5) > 1e-6:
                print("ERROR: car planning {}: expected one 3.5 kWh slot at {}, got {}".format(label, expected_start, plan))
                failed = True
                continue
            # The slot keeps the plain import rate for the car premium, whatever it was chosen on
            expected_average = 15.0 if expected_start == 25 * 60 else 30.0
            if abs(plan[0]["average"] - expected_average) > 1e-9:
                print("ERROR: car planning {}: slot average {} expected the import rate {}".format(label, plan[0]["average"], expected_average))
                failed = True

        # A price cap applies to the netted price
        my_predbat.metric_net_settlement_window_minutes = 60
        my_predbat.net_settlement_surplus = net_settlement_surplus_record({12: 8.0}, 60, my_predbat.midnight_utc)
        my_predbat.car_charging_plan_smart = [True]
        my_predbat.car_charging_plan_max_price = [10]
        plan = my_predbat.plan_car_charging(0, low_rates)
        if len(plan) != 1 or plan[0]["start"] != 12 * 60:
            print("ERROR: car planning max price 10p should still allow the 5p surplus slot, got {}".format(plan))
            failed = True
    finally:
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)
        if saved_log is None:
            del my_predbat.log
        else:
            my_predbat.log = saved_log
    return failed


def run_net_settlement_window_tests(my_predbat):
    """Run the net-settlement-aware iBoost and car charging tests, returns True on failure"""
    print("**** Running net settlement window-aware iBoost and car tests ****")
    failed = False
    failed |= run_net_settlement_helper_tests(my_predbat)
    failed |= run_net_settlement_surplus_recording_tests(my_predbat)
    failed |= run_net_settlement_iboost_gate_tests(my_predbat)
    failed |= run_net_settlement_iboost_smart_tests(my_predbat)
    failed |= run_net_settlement_car_tests(my_predbat)
    if not failed:
        print("PASS")
    return failed

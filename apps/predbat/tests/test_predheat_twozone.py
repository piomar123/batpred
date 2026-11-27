# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for Predheat's two-zone model (predheat_twozone.py) and its wiring into PredHeat."""

from predheat_twozone import parse_two_zone_config, parse_dhw_active, simulate_two_zone, controller_reading, valve_factor
from predheat import PredHeat
from datetime import datetime, timedelta, timezone

COP_TABLE = {-20: 1.8, -18: 1.88, -16: 1.96, -14: 2.04, -12: 2.12, -10: 2.2, -8: 2.32, -6: 2.44, -4: 2.58, -2: 2.74, 0: 2.9, 2: 3.08, 4: 3.25, 6: 3.65, 8: 4.2, 10: 4.8, 12: 5.2, 14: 5.4, 16: 5.5, 18: 5.5, 20: 5.5}
WEATHER_COMP = {-20: 45, 7: 35, 20: 35}

GOLDEN_CONFIG = {
    "coupling_watts": 203,
    "zone1": {
        "internal_temperature": "sensor.predheat_zone1_room",
        "target_temperature": "sensor.predheat_zone1_setpoint",
        "heat_loss_watts": 156,
        "heat_gain_static": 781,
        "heat_capacity_kwh": 8.0,
        "heat_output": 1440,
        "heat_output_delta": 14,
        "hysteresis": 0.0,
        "hysteresis_off": 0.6,
    },
    "zone2": {"heat_loss_watts": 81, "heat_gain_static": 929, "heat_capacity_kwh": 6.5, "heat_output": 2450, "hysteresis": 0.26, "hysteresis_off": 0.20},
}


def _fill(table):
    """Linear fill between integer keys, rounded to 4 places, as PredHeat.fill_table_gaps does."""
    keys = sorted(table)
    out = {}
    for key in range(keys[0], keys[-1] + 1):
        low = max(k for k in keys if k <= key)
        high = min(k for k in keys if k >= key)
        out[key] = table[low] if low == high else round(table[low] + (table[high] - table[low]) * (key - low) / (high - low), 4)
    return out


def _zone(**overrides):
    """A zone dict as parse_two_zone_config returns it, with simple defaults."""
    zone = {
        "name": "z",
        "heat_loss_watts": 100.0,
        "heat_gain_static": 0.0,
        "heat_capacity_kwh": 2.0,
        "heat_output": 0.0,
        "heat_output_delta": None,
        "hysteresis": 0.5,
        "hysteresis_off": 0.1,
        "internal_temperature": None,
        "target_temperature": None,
        "heating_active": None,
    }
    zone.update(overrides)
    return zone


def _constant(value):
    """A callable of the minute returning a constant."""
    return lambda minute: value


def _kwh(watts_list, step=5):
    """Energy in kWh of a list of per-step powers in W."""
    return sum(watts_list) * step / 60.0 / 1000.0


def check_parse():
    """parse_two_zone_config: valid config, defaults, and errors instead of exceptions."""
    failed = False
    zones, coupling, errors = parse_two_zone_config(GOLDEN_CONFIG)
    if errors or coupling != 203 or zones[0]["heat_output_delta"] != 14 or zones[1]["heat_output_delta"] is not None:
        print("  ERROR: golden config did not parse as expected: {} {} {}".format(zones, coupling, errors))
        failed = True
    if zones and (zones[1]["internal_temperature"] is not None or zones[0]["heating_active"] is not None):
        print("  ERROR: absent entity keys should be None, got {}".format(zones))
        failed = True

    minimal = {"zone1": {"internal_temperature": "sensor.a", "target_temperature": "sensor.b", "heat_loss_watts": 10, "heat_capacity_kwh": 1, "heat_output": 1000}, "zone2": {"heat_loss_watts": 10, "heat_capacity_kwh": 1, "heat_output": 1000}}
    zones, coupling, errors = parse_two_zone_config(minimal)
    if errors or coupling != 0 or zones[0]["hysteresis"] != 0.5 or zones[0]["hysteresis_off"] != 0.1 or zones[0]["heat_gain_static"] != 0:
        print("  ERROR: defaults not applied: {} {} {}".format(zones, coupling, errors))
        failed = True

    bad_cases = [
        (None, "two_zone must be a dictionary"),
        ("yes", "two_zone must be a dictionary"),
        ({"zone1": minimal["zone1"]}, "zone2 is missing"),
        ({"zone1": "x", "zone2": minimal["zone2"]}, "zone1 is missing"),
        ({"zone1": dict(minimal["zone1"], heat_capacity_kwh=0), "zone2": minimal["zone2"]}, "zone1.heat_capacity_kwh must be above 0"),
        ({"zone1": dict(minimal["zone1"], heat_loss_watts="lots"), "zone2": minimal["zone2"]}, "zone1.heat_loss_watts must be a number"),
        ({"zone1": dict(minimal["zone1"], target_temperature=None), "zone2": minimal["zone2"]}, "zone1.target_temperature is required"),
        ({"zone1": minimal["zone1"], "zone2": {"heat_loss_watts": 10, "heat_capacity_kwh": 1}}, "zone2.heat_output is required"),
        ({"zone1": minimal["zone1"], "zone2": minimal["zone2"], "coupling_watts": -5}, "coupling_watts must be at least 0"),
        ({"zone1": dict(minimal["zone1"], heat_output_delta=0), "zone2": minimal["zone2"]}, "zone1.heat_output_delta must be above 0"),
        ({"zone1": dict(minimal["zone1"], heating_active=5), "zone2": minimal["zone2"]}, "zone1.heating_active must be an entity id"),
    ]
    for cfg, expected in bad_cases:
        zones, coupling, errors = parse_two_zone_config(cfg)
        if zones is not None or not any(expected in error for error in errors):
            print("  ERROR: config {} should give an error containing '{}', got {} {}".format(cfg, expected, zones, errors))
            failed = True
    failed |= check_parse_refit_keys(minimal)
    return failed


def check_parse_refit_keys(minimal):
    """parse_two_zone_config / parse_dhw_active: the optional controller, valve and dhw_active keys."""
    failed = False
    zones, _, errors = parse_two_zone_config(minimal)
    if errors or any(zone["controller"] is not None or zone["valve"] is not None for zone in zones):
        print("  ERROR: without the keys controller and valve should be None, got {} {}".format(zones, errors))
        failed = True

    zone2 = dict(minimal["zone2"], controller={"offset": 1.49, "slope": 1.09, "resolution": 1.0})
    zones, _, errors = parse_two_zone_config({"zone1": minimal["zone1"], "zone2": zone2})
    if errors or zones[1]["controller"] != {"offset": 1.49, "slope": 1.09, "resolution": 1.0}:
        print("  ERROR: zone2 controller did not parse: {} {}".format(zones, errors))
        failed = True
    zones, _, errors = parse_two_zone_config({"zone1": minimal["zone1"], "zone2": dict(minimal["zone2"], controller={})})
    if errors or zones[1]["controller"] != {"offset": 0.0, "slope": 1.0, "resolution": 0.0}:
        print("  ERROR: controller defaults not applied: {} {}".format(zones, errors))
        failed = True
    zones, _, errors = parse_two_zone_config({"zone1": dict(minimal["zone1"], valve={"temperature": 20.7, "band": 1.0}), "zone2": minimal["zone2"]})
    if errors or zones[0]["valve"] != {"temperature": 20.7, "band": 1.0}:
        print("  ERROR: zone1 valve did not parse: {} {}".format(zones, errors))
        failed = True

    bad_cases = [
        ({"zone1": minimal["zone1"], "zone2": dict(minimal["zone2"], controller={"slope": 0})}, "zone2.controller.slope must be above 0"),
        ({"zone1": minimal["zone1"], "zone2": dict(minimal["zone2"], controller={"resolution": -1})}, "zone2.controller.resolution must be at least 0"),
        ({"zone1": minimal["zone1"], "zone2": dict(minimal["zone2"], controller=5)}, "zone2.controller must be a dictionary"),
        ({"zone1": dict(minimal["zone1"], valve={"temperature": 20.7, "band": 0}), "zone2": minimal["zone2"]}, "zone1.valve.band must be above 0"),
        ({"zone1": dict(minimal["zone1"], valve={"band": 1.0}), "zone2": minimal["zone2"]}, "zone1.valve.temperature is required"),
        ({"zone1": dict(minimal["zone1"], valve="x"), "zone2": minimal["zone2"]}, "zone1.valve must be a dictionary"),
        ({"zone1": minimal["zone1"], "zone2": minimal["zone2"], "dhw_active": 3}, "dhw_active must be an entity id"),
    ]
    for cfg, expected in bad_cases:
        zones, _, errors = parse_two_zone_config(cfg)
        if zones is not None or not any(expected in error for error in errors):
            print("  ERROR: config {} should give an error containing '{}', got {} {}".format(cfg, expected, zones, errors))
            failed = True

    for cfg, expected in (({"dhw_active": "sensor.aquarea_dhw_running"}, ("sensor.aquarea_dhw_running", [])), ({}, (None, [])), ({"dhw_active": None}, (None, []))):
        got = parse_dhw_active(cfg)
        if got != expected:
            print("  ERROR: parse_dhw_active({}) = {}, expected {}".format(cfg, got, expected))
            failed = True
    entity, errors = parse_dhw_active({"dhw_active": 3})
    if entity is not None or not errors:
        print("  ERROR: parse_dhw_active with a number should give an error, got {} {}".format(entity, errors))
        failed = True
    return failed


def check_relaxation():
    """No heating, constant outside: each zone settles at outside + gain / loss; zero coupling keeps zones independent."""
    failed = False
    zones = [_zone(heat_loss_watts=100, heat_gain_static=500, heat_capacity_kwh=2.0, hysteresis_off=99), _zone(heat_loss_watts=50, heat_gain_static=0, heat_capacity_kwh=1.0, hysteresis_off=99)]
    result = simulate_two_zone(zones, 0, (20.0, 15.0), (False, False), _constant(5.0), (_constant(0.0), _constant(0.0)), _constant(35), _constant(4.0), 7000, 10 * 1440)
    end = (result["temp"][0][-1], result["temp"][1][-1])
    if abs(end[0] - 10.0) > 0.01 or abs(end[1] - 5.0) > 0.01:
        print("  ERROR: expected the zones to settle at 10.0 and 5.0, got {}".format(end))
        failed = True
    if any(result["heating"][0]) or any(result["heating"][1]) or sum(result["electric_watts"]) != 0:
        print("  ERROR: heating ran although no thermostat should call")
        failed = True
    # One step by hand: zone 1 loses 100 W x 15 K, gains 500 W, for 5 minutes, into 2 kWh/K
    expected_first = 20.0 + (500 - 100 * 15) / 1000.0 * (5 / 60.0) / 2.0
    if abs(result["temp"][0][0] - expected_first) > 1e-9:
        print("  ERROR: first step {} expected {}".format(result["temp"][0][0], expected_first))
        failed = True
    return failed


def check_coupling():
    """With no outside loss or gains the heat-capacity-weighted mean is conserved while the zones converge."""
    failed = False
    zones = [_zone(heat_loss_watts=0, heat_capacity_kwh=2.0, hysteresis_off=99), _zone(heat_loss_watts=0, heat_capacity_kwh=4.0, hysteresis_off=99)]
    result = simulate_two_zone(zones, 100, (20.0, 26.0), (False, False), _constant(0.0), (_constant(0.0), _constant(0.0)), _constant(35), _constant(4.0), 7000, 5 * 1440)
    for step in (0, 100, len(result["minute"]) - 1):
        mean = (2.0 * result["temp"][0][step] + 4.0 * result["temp"][1][step]) / 6.0
        if abs(mean - 24.0) > 1e-9:
            print("  ERROR: weighted mean {} at step {} should stay 24.0".format(mean, step))
            failed = True
    if abs(result["temp"][0][-1] - 24.0) > 0.01 or abs(result["temp"][1][-1] - 24.0) > 0.01:
        print("  ERROR: zones should converge to 24.0, got {} {}".format(result["temp"][0][-1], result["temp"][1][-1]))
        failed = True
    if result["temp"][0][0] <= 20.0 or result["temp"][1][0] >= 26.0:
        print("  ERROR: heat should flow from the warmer zone 2 into zone 1")
        failed = True
    return failed


def check_thermostats():
    """Each zone starts at target - hysteresis_off and stops at target + hysteresis, independently of the other."""
    failed = False
    zone_a = _zone(heat_loss_watts=200, heat_capacity_kwh=1.0, heat_output=3000, hysteresis=0.2, hysteresis_off=0.5)
    zone_b = _zone(heat_loss_watts=200, heat_capacity_kwh=1.0, heat_output=3000, hysteresis_off=99)
    result = simulate_two_zone([zone_a, zone_b], 0, (20.6, 20.6), (False, False), _constant(15.0), (_constant(21.0), _constant(21.0)), _constant(35), _constant(4.0), 0, 1440)
    heating, temps = result["heating"][0], result["temp"][0]
    switch_on = [i for i in range(2, len(heating)) if heating[i] and not heating[i - 1]]
    switch_off = [i for i in range(2, len(heating)) if not heating[i] and heating[i - 1]]
    if not switch_on or not switch_off:
        print("  ERROR: zone 1 should cycle, on at {} off at {}".format(switch_on, switch_off))
        failed = True
    for i in switch_on:
        # The decision at step i uses the temperature left by step i-1
        if not (temps[i - 1] <= 20.5 < temps[i - 2]):
            print("  ERROR: zone 1 switched on at {} with temperature {} (previous {})".format(i, temps[i - 1], temps[i - 2]))
            failed = True
    for i in switch_off:
        if not (temps[i - 1] >= 21.2 > temps[i - 2]):
            print("  ERROR: zone 1 switched off at {} with temperature {} (previous {})".format(i, temps[i - 1], temps[i - 2]))
            failed = True
    if any(result["heating"][1]):
        print("  ERROR: zone 2 (start threshold 99 below target) must never heat")
        failed = True
    # A zone that starts as heating keeps heating at the first step while below its stop threshold
    result = simulate_two_zone([zone_a, zone_b], 0, (20.8, 20.8), (True, True), _constant(15.0), (_constant(21.0), _constant(21.0)), _constant(35), _constant(4.0), 0, 10)
    if result["heating"][0][0] is not True or result["heating"][1][0] is not True:
        print("  ERROR: start_heating should carry into the first step")
        failed = True
    return failed


def check_outputs():
    """Proportional and fixed emitter output, the total cap scaling both zones, and electricity = heat / COP."""
    failed = False
    proportional = _zone(heat_loss_watts=0, heat_capacity_kwh=1000.0, heat_output=1400, heat_output_delta=14, hysteresis=99, hysteresis_off=-99)
    fixed = _zone(heat_loss_watts=0, heat_capacity_kwh=1000.0, heat_output=2000, hysteresis=99, hysteresis_off=-99)
    result = simulate_two_zone([proportional, fixed], 0, (20.0, 20.0), (True, True), _constant(10.0), (_constant(21.0), _constant(21.0)), _constant(34.0), _constant(4.0), 0, 5)
    if abs(result["heat_watts"][0][0] - 1400) > 1e-6 or result["heat_watts"][1][0] != 2000:
        print("  ERROR: expected 1400 W (14 K at 100 W/K) and 2000 W, got {} {}".format(result["heat_watts"][0][0], result["heat_watts"][1][0]))
        failed = True
    if abs(result["electric_watts"][0] - 3400 / 4.0) > 1e-6:
        print("  ERROR: electricity should be heat / COP = 850 W, got {}".format(result["electric_watts"][0]))
        failed = True
    result = simulate_two_zone([proportional, fixed], 0, (40.0, 20.0), (True, True), _constant(10.0), (_constant(50.0), _constant(21.0)), _constant(34.0), _constant(4.0), 0, 5)
    if result["heat_watts"][0][0] != 0:
        print("  ERROR: a room warmer than the flow gets no heat, got {}".format(result["heat_watts"][0][0]))
        failed = True
    result = simulate_two_zone([proportional, fixed], 0, (20.0, 20.0), (True, True), _constant(10.0), (_constant(21.0), _constant(21.0)), _constant(34.0), _constant(4.0), 1700, 5)
    if abs(result["heat_watts"][0][0] - 700) > 1e-6 or abs(result["heat_watts"][1][0] - 1000) > 1e-6:
        print("  ERROR: a 1700 W cap should halve both outputs, got {} {}".format(result["heat_watts"][0][0], result["heat_watts"][1][0]))
        failed = True
    # COP and flow are looked up at the outside temperature rounded to a whole degree and clamped to -20..20
    seen = []
    simulate_two_zone([proportional, fixed], 0, (20.0, 20.0), (True, True), lambda minute: -30.0 if minute == 0 else 7.4, (_constant(21.0), _constant(21.0)), lambda out: seen.append(out) or 34.0, lambda out: seen.append(out) or 4.0, 0, 10)
    if seen != [-20, -20, 7, 7]:
        print("  ERROR: expected lookups at -20 then 7, got {}".format(seen))
        failed = True
    return failed


def check_golden():
    """48 h with the fitted parameters matches the validated prototype (scratch twozone.py, 2026-11-03)."""
    failed = False
    zones, coupling, errors = parse_two_zone_config(GOLDEN_CONFIG)
    cop = _fill(COP_TABLE)
    flow = _fill(WEATHER_COMP)

    def outside(minute):
        return 9.0 if 8 <= (minute // 60) % 24 < 20 else 4.0

    def target1(minute):
        hour = (minute // 60) % 24
        return 21.0 if (3 <= hour < 6 or 17 <= hour < 20) else 20.4

    def target2(minute):
        return 23.0 if 3 <= (minute // 60) % 24 < 19 else 22.0

    result = simulate_two_zone(zones, coupling, (20.2, 22.6), (False, False), outside, (target1, target2), lambda out: flow[out], lambda out: cop[out], 7000, 2880)
    checks = [
        ("steps", len(result["minute"]), 576, 0),
        ("electricity kWh", _kwh(result["electric_watts"]), 20.9433, 0.0005),
        ("heat kWh", _kwh(result["heat_watts"][0]) + _kwh(result["heat_watts"][1]), 76.4688, 0.0005),
        ("zone 1 heating steps", sum(result["heating"][0]), 272, 0),
        ("zone 2 heating steps", sum(result["heating"][1]), 197, 0),
        ("zone 1 first heating step", result["heating"][0].index(True), 32, 0),
        ("zone 2 first heating step", result["heating"][1].index(True), 36, 0),
        ("zone 1 temp at 6 h", result["temp"][0][71], 19.9912, 0.0005),
        ("zone 2 temp at 6 h", result["temp"][1][71], 22.7515, 0.0005),
        ("zone 1 temp at 48 h", result["temp"][0][-1], 19.8065, 0.0005),
        ("zone 2 temp at 48 h", result["temp"][1][-1], 22.0731, 0.0005),
    ]
    for name, got, expected, tolerance in checks:
        if abs(got - expected) > tolerance:
            print("  ERROR: golden {}: got {} expected {}".format(name, got, expected))
            failed = True
    return failed


def _always_heating(dhw=None):
    """Both zones at 1 kW fixed output, targets far above the room, outside 0C, for 24 hours."""
    zones = [_zone(heat_output=1000.0), _zone(heat_output=1000.0)]
    return simulate_two_zone(zones, 0, (20.0, 20.0), (True, True), _constant(0.0), (_constant(30.0), _constant(30.0)), _constant(35), _constant(4.0), 7000, 1440, dhw=dhw)


def check_dhw():
    """The hot-water fraction pauses both zones' output without touching the thermostats, clamped to 0..1."""
    failed = False
    plain = _always_heating()
    paused = _always_heating(dhw=lambda minute: 1.0 if minute < 60 else 0.0)
    first_hour = range(12)
    if any(paused["heat_watts"][index][step] != 0 for index in (0, 1) for step in first_hour) or any(paused["electric_watts"][step] != 0 for step in first_hour):
        print("  ERROR: hot water should stop all output in the first hour, got {}".format(paused["heat_watts"][0][:12]))
        failed = True
    if not all(paused["heating"][index][step] for index in (0, 1) for step in first_hour):
        print("  ERROR: hot water must not change the thermostat state")
        failed = True
    if not _kwh(paused["electric_watts"]) < _kwh(plain["electric_watts"]):
        print("  ERROR: the pause should lower the day's electricity: {} vs {}".format(_kwh(paused["electric_watts"]), _kwh(plain["electric_watts"])))
        failed = True
    if paused["dhw"][:12] != [1.0] * 12 or paused["dhw"][12] != 0.0 or plain["dhw"] != [0.0] * len(plain["minute"]):
        print("  ERROR: result dhw should hold the fraction used per step, got {} / {}".format(paused["dhw"][:13], plain["dhw"][:3]))
        failed = True
    over = _always_heating(dhw=_constant(1.7))
    if sum(over["electric_watts"]) != 0 or over["dhw"][0] != 1.0:
        print("  ERROR: a fraction above 1 should be treated as 1, got {} W".format(sum(over["electric_watts"])))
        failed = True
    under = _always_heating(dhw=_constant(-0.5))
    if under["electric_watts"] != plain["electric_watts"] or under["dhw"][0] != 0.0:
        print("  ERROR: a fraction below 0 should be treated as 0")
        failed = True
    half = _always_heating(dhw=_constant(0.5))
    if abs(half["heat_watts"][0][0] - 500.0) > 1e-9:
        print("  ERROR: a fraction of 0.5 should halve the output, got {}".format(half["heat_watts"][0][0]))
        failed = True
    return failed


def check_controller():
    """controller_reading maps and rounds the room temperature; zone 2's thermostat switches on the reading."""
    failed = False
    remote = {"controller": {"offset": 1.49, "slope": 1.09, "resolution": 1.0}}
    cases = [
        ({"controller": None}, 20.37, 20.37),
        (remote, 20.5, 24.0),
        ({"controller": {"offset": 23.5, "slope": 1.0, "resolution": 1.0}}, 0.0, 24.0),
        ({"controller": {"offset": 22.5, "slope": 1.0, "resolution": 1.0}}, 0.0, 23.0),
        ({"controller": {"offset": 1.0, "slope": 2.0, "resolution": 0.0}}, 10.3, 21.6),
        ({"controller": {"offset": 0.0, "slope": 1.0, "resolution": 0.5}}, 20.3, 20.5),
    ]
    for zone, temp, expected in cases:
        got = controller_reading(zone, temp)
        if abs(got - expected) > 1e-9:
            print("  ERROR: controller_reading({}, {}) = {}, expected {}".format(zone["controller"], temp, got, expected))
            failed = True

    # Zone 2 cools from 21.3C with the remote at target 24 and no hysteresis: the reading first reaches 24 below 21.110C
    # (1.49 + 1.09 x 21.110 = 24.5); on the room's own scale it would have started at once.
    zones = [_zone(hysteresis_off=99), _zone(heat_output=3000.0, hysteresis=0.0, hysteresis_off=0.0, controller=remote["controller"])]
    result = simulate_two_zone(zones, 0, (20.0, 21.3), (False, False), _constant(0.0), (_constant(0.0), _constant(24.0)), _constant(35), _constant(4.0), 7000, 240)
    first = result["heating"][1].index(True) if True in result["heating"][1] else None
    before = None if not first else result["temp"][1][first - 1]
    if before is None or not (21.02 < before <= 21.110):
        print("  ERROR: zone 2 should start heating once the room is just below 21.11C, started at step {} from {}".format(first, before))
        failed = True
    return failed


def check_valve():
    """valve_factor: full output below the band, none above it, linear in between."""
    failed = False
    zone = {"valve": {"temperature": 20.7, "band": 1.0}}
    for temp, expected in ((20.0, 1.0), (20.2, 1.0), (20.7, 0.5), (20.95, 0.25), (21.2, 0.0), (21.3, 0.0)):
        got = valve_factor(zone, temp)
        if abs(got - expected) > 1e-9:
            print("  ERROR: valve_factor at {} = {}, expected {}".format(temp, got, expected))
            failed = True
    if valve_factor({"valve": None}, 30.0) != 1.0:
        print("  ERROR: without a valve the factor should be 1")
        failed = True
    zones = [_zone(heat_output=1000.0, valve={"temperature": 20.7, "band": 1.0}), _zone()]
    result = simulate_two_zone(zones, 0, (20.7, 20.0), (True, False), _constant(0.0), (_constant(30.0), _constant(0.0)), _constant(35), _constant(4.0), 7000, 5)
    if abs(result["heat_watts"][0][0] - 500.0) > 1e-9 or not result["heating"][0][0]:
        print("  ERROR: zone 1 at the valve temperature should deliver half its output and keep calling, got {}".format(result["heat_watts"][0][0]))
        failed = True
    return failed


def run_pure_checks():
    """All the pure-model checks; True when any failed."""
    failed = False
    for name, check in (("parse", check_parse), ("relaxation", check_relaxation), ("coupling", check_coupling), ("thermostats", check_thermostats), ("outputs", check_outputs), ("golden", check_golden), ("dhw", check_dhw), ("controller", check_controller), ("valve", check_valve)):
        print("Test: predheat two-zone {}".format(name))
        failed = check() or failed
    return failed


# ---------------------------------------------------------------------------
# Predheat wiring (Task 3): update_pred with faked history, weather and state
# ---------------------------------------------------------------------------

BASE_ARGS = {
    "mode": "pump",
    "external_temperature": "sensor.outside",
    "internal_temperature": "sensor.room2",
    "target_temperature": "sensor.target2",
    "heating_energy": "sensor.heat_energy",
    "weather": "weather.home",
    "days_previous": [1],
    "forecast_days": 2,
    "heat_pump_efficiency": COP_TABLE,
    "heat_cop": 5.5,
    "weather_compensation": WEATHER_COMP,
    "heat_max_power": 7000,
    "heat_min_power": 2000,
    "heat_loss_watts": 220,
    "heat_loss_degrees": 0.008,
    "heat_gain_static": 1500,
    "heat_output": 14000,
    "heat_volume": 180,
}
HISTORY = {
    "sensor.outside": 5.0,
    "sensor.room2": 22.5,
    "sensor.target2": 23.0,
    "sensor.heat_energy": 0.0,
    "sensor.zone1_room": 20.0,
    "sensor.zone1_target": 21.0,
}


def _two_zone_args(**zone1_overrides):
    """BASE_ARGS plus the golden two_zone block pointed at the fake zone 1 entities."""
    zone1 = dict(GOLDEN_CONFIG["zone1"], internal_temperature="sensor.zone1_room", target_temperature="sensor.zone1_target", heating_active="binary_sensor.z1_pump")
    zone1.update(zone1_overrides)
    return dict(BASE_ARGS, two_zone={"coupling_watts": GOLDEN_CONFIG["coupling_watts"], "zone1": zone1, "zone2": dict(GOLDEN_CONFIG["zone2"])})


def _run_update(my_predbat, predheat_args, history=None, states_now=None, zone1_room_items=None):
    """
    Run PredHeat.update_pred against faked inputs; returns (published states, log lines)

    The current states default to the zone 1 history values and a running zone 1 pump; states_now overrides
    them. zone1_room_items, when given, are the raw Home Assistant history of sensor.zone1_room, read through
    the real minute_data_entity. record_status messages are logged as "STATUS: <message>".
    """
    history = HISTORY if history is None else history
    current = {name: str(history[name]) for name in ("sensor.zone1_room", "sensor.zone1_target") if name in history}
    current["binary_sensor.z1_pump"] = "on"
    current.update(states_now or {})
    states_now = current
    published = {}
    logs = []
    original_log = my_predbat.log
    original_args = my_predbat.args.get("predheat", None)
    had_rate_import = hasattr(my_predbat, "rate_import")
    original_rate_import = getattr(my_predbat, "rate_import", None)
    my_predbat.args["predheat"] = predheat_args
    my_predbat.log = lambda msg, quiet=True: logs.append(msg)
    my_predbat.rate_import = {}
    try:
        predheat = PredHeat(my_predbat)
        predheat.reset()
        predheat.set_state = lambda entity_id, state=None, attributes=None, **kwargs: published.__setitem__(entity_id, (state, attributes or {}))
        predheat.expose_config = lambda *args, **kwargs: None
        predheat.record_status = lambda message, *args, **kwargs: logs.append("STATUS: {}".format(message))
        predheat.get_state = lambda entity_id=None, default=None, **kwargs: states_now.get(entity_id, default)
        predheat.today_cost = lambda data: 0

        real_minute_data = predheat.minute_data_entity
        predheat.get_history = lambda entity_id=None, days=30, **kwargs: [list(zone1_room_items)] if entity_id == "sensor.zone1_room" else []

        def fake_minute_data(now_utc, key, incrementing=False, smoothing=False, scaling=1.0, entity_ids=None, **kwargs):
            """Constant history for the fake entities, as minute -> value going back three days."""
            name = entity_ids if entity_ids is not None else predheat_args.get(key)
            if zone1_room_items is not None and name == "sensor.zone1_room":
                return real_minute_data(now_utc, key, incrementing=incrementing, smoothing=smoothing, scaling=scaling, entity_ids=entity_ids, **kwargs)
            if name not in history:
                return {}, 0
            return {minute: history[name] for minute in range(0, 3 * 24 * 60)}, 3

        def fake_weather(now_utc):
            """No forecast: Predheat holds the current outside temperature."""
            predheat.temperatures = {}

        predheat.minute_data_entity = fake_minute_data
        predheat.get_weather_data = fake_weather
        predheat.update_pred(scheduled=True)
    finally:
        my_predbat.log = original_log
        if original_args is None:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = original_args
        if had_rate_import:
            my_predbat.rate_import = original_rate_import
        else:
            del my_predbat.rate_import
    return published, logs


def _expected_energy_h8(heat_cop=5.5, zone1_heating=True, zone1_room=20.0):
    """predheat.heat_energy_h8 for the faked inputs, straight from simulate_two_zone."""
    zones, coupling, _ = parse_two_zone_config(_two_zone_args()["two_zone"])
    cop = _fill(COP_TABLE)
    flow = _fill(WEATHER_COMP)
    result = simulate_two_zone(zones, coupling, (zone1_room, 22.5), (zone1_heating, False), _constant(5.0), (_constant(21.0), _constant(23.0)), lambda out: flow[out], lambda out: heat_cop * cop[out] / max(cop.values()), 7000, 2 * 24 * 60)
    return round(sum(result["electric_watts"][:97]) * 5 / 60.0 / 1000.0, 2)


def check_wiring(my_predbat):
    """update_pred runs the two-zone model when configured and falls back to single zone otherwise."""
    failed = False

    print("Test: predheat two-zone publishes both zones and the total energy")
    published, logs = _run_update(my_predbat, _two_zone_args())
    for entity in ("predheat.heat_energy", "predheat.heat_energy_h8", "predheat.internal_temp", "predheat.internal_temp_zone1", "predheat.target_temp_zone1", "predheat.cost"):
        if entity not in published:
            print("  ERROR: {} was not published, got {}".format(entity, sorted(published)))
            failed = True
    if "predheat.volume_temp" in published:
        print("  ERROR: volume_temp should not be published in two-zone mode")
        failed = True
    if not failed:
        if published["predheat.internal_temp_zone1"][0] != 20.0 or published["predheat.target_temp_zone1"][0] != 21.0 or published["predheat.internal_temp"][0] != 22.5:
            print("  ERROR: current zone states wrong: {} {} {}".format(published["predheat.internal_temp_zone1"][0], published["predheat.target_temp_zone1"][0], published["predheat.internal_temp"][0]))
            failed = True
        if not published["predheat.heat_energy"][1].get("external") or not published["predheat.internal_temp_zone1"][1].get("results"):
            print("  ERROR: heat_energy needs its external attribute and zone 1 its results forecast")
            failed = True
        expected = _expected_energy_h8()
        if published["predheat.heat_energy_h8"][0] != expected or expected <= 0:
            print("  ERROR: heat_energy_h8 {} expected {} (> 0)".format(published["predheat.heat_energy_h8"][0], expected))
            failed = True
    if not any("two_zone enabled" in line for line in logs):
        print("  ERROR: expected a 'two_zone enabled' log line, got {}".format(logs))
        failed = True

    print("Test: predheat two-zone electricity follows heat_cop like single zone")
    published, logs = _run_update(my_predbat, dict(_two_zone_args(), heat_cop=4.4))
    expected = _expected_energy_h8(heat_cop=4.4)
    if published.get("predheat.heat_energy_h8", (None,))[0] != expected:
        print("  ERROR: with heat_cop 4.4 heat_energy_h8 {} expected {}".format(published.get("predheat.heat_energy_h8"), expected))
        failed = True

    print("Test: predheat two-zone treats an unavailable heating_active as not heating")
    # 20.6 sits between zone 1's start (20.4) and stop (21.0) thresholds, so the starting state decides
    warm_zone1 = dict(HISTORY, **{"sensor.zone1_room": 20.6})
    expected_off = _expected_energy_h8(zone1_heating=False, zone1_room=20.6)
    expected_on = _expected_energy_h8(zone1_heating=True, zone1_room=20.6)
    published, logs = _run_update(my_predbat, _two_zone_args(), history=warm_zone1, states_now={"binary_sensor.z1_pump": "unavailable"})
    if expected_off == expected_on or published.get("predheat.heat_energy_h8", (None,))[0] != expected_off:
        print("  ERROR: heat_energy_h8 {} expected {} with zone 1 starting off (on would be {})".format(published.get("predheat.heat_energy_h8"), expected_off, expected_on))
        failed = True
    published, logs = _run_update(my_predbat, _two_zone_args(), history=warm_zone1)
    if published.get("predheat.heat_energy_h8", (None,))[0] != expected_on:
        print("  ERROR: heat_energy_h8 {} expected {} with zone 1's pump on".format(published.get("predheat.heat_energy_h8"), expected_on))
        failed = True

    fallbacks = [
        ("a broken two_zone block", dict(BASE_ARGS, two_zone={"zone1": _two_zone_args()["two_zone"]["zone1"]}), HISTORY, "zone2 is missing"),
        ("zone 1 without history", _two_zone_args(), {k: v for k, v in HISTORY.items() if k != "sensor.zone1_room"}, "no history"),
        ("gas mode", dict(_two_zone_args(), mode="gas"), HISTORY, "mode gas is not supported", {}, None),
        ("zone 1's room sensor unavailable now", _two_zone_args(), HISTORY, "zone1 internal_temperature sensor.zone1_room is unavailable", {"sensor.zone1_room": "unavailable"}, None),
        (
            "zone 1's room sensor unavailable through its whole history",
            _two_zone_args(),
            HISTORY,
            "zone1 internal_temperature sensor.zone1_room is unavailable",
            {"sensor.zone1_room": "unavailable"},
            [{"state": "unavailable", "last_updated": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()}],
        ),
        ("zone 1's target unknown now", _two_zone_args(), HISTORY, "zone1 target_temperature sensor.zone1_target is unknown", {"sensor.zone1_target": "unknown"}, None),
    ]
    # "zone 1 without history" is a sensor that exists (it has a state) but has no history yet, like a new template sensor
    fallbacks[:2] = [fallbacks[0] + ({}, None), fallbacks[1] + ({"sensor.zone1_room": "20.0"}, None)]
    for label, args, history, reason, states_now, zone1_room_items in fallbacks:
        print("Test: predheat two-zone falls back to single zone with {}".format(label))
        published, logs = _run_update(my_predbat, args, history=history, states_now=states_now, zone1_room_items=zone1_room_items)
        warnings = [line for line in logs if line.startswith("Warn: Predheat: two_zone ignored")]
        if not warnings or reason not in warnings[0]:
            print("  ERROR: expected a 'two_zone ignored' warning naming '{}', got {}".format(reason, [line for line in logs if "two_zone" in line]))
            failed = True
        if not any(line.startswith("STATUS: Warn: Predheat: two_zone ignored") for line in logs):
            print("  ERROR: the fallback should also be reported with record_status, got {}".format([line for line in logs if line.startswith("STATUS")]))
            failed = True
        if "predheat.volume_temp" not in published or "predheat.internal_temp_zone1" in published:
            print("  ERROR: expected the single-zone outputs, got {}".format(sorted(published)))
            failed = True

    print("Test: predheat two-zone warns that smart_thermostat is ignored")
    published, logs = _run_update(my_predbat, dict(_two_zone_args(), smart_thermostat=True))
    if not any("smart_thermostat is not supported with two_zone" in line for line in logs) or "predheat.internal_temp_zone1" not in published:
        print("  ERROR: expected two-zone outputs and a smart_thermostat warning, got {}".format(logs))
        failed = True

    print("Test: predheat without two_zone is unchanged single zone")
    published, logs = _run_update(my_predbat, dict(BASE_ARGS))
    if "predheat.volume_temp" not in published or "predheat.internal_temp_zone1" in published or any("two_zone" in line for line in logs):
        print("  ERROR: expected the plain single-zone run, got {} {}".format(sorted(published), [line for line in logs if "two_zone" in line]))
        failed = True
    return failed


def test_predheat_twozone(my_predbat):
    """Predheat two-zone model and its wiring into PredHeat."""
    print("**** Running Predheat two-zone tests ****")
    failed = run_pure_checks()
    failed = check_wiring(my_predbat) or failed
    return failed

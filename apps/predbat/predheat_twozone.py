# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Two-zone heating simulation for Predheat.

Two rooms, each with its own thermostat, heat loss to outside, static gains and heat capacity, exchange heat
with each other in proportion to their temperature difference. While a zone's thermostat calls for heat its
emitters deliver either a fixed output or one proportional to the flow temperature minus the room temperature.
One heat pump serves both zones; its electrical draw is the total heat divided by the COP for the outside
temperature. Pure functions: no Home Assistant or Predbat calls, so the offline fitting tool can run exactly the
same model as the live forecast.
"""

ZONE_NAMES = ("zone1", "zone2")
REQUIRED_KEYS = ("heat_loss_watts", "heat_capacity_kwh", "heat_output")
ENTITY_KEYS = ("internal_temperature", "target_temperature", "heating_active")
DEFAULT_HYSTERESIS = 0.5
DEFAULT_HYSTERESIS_OFF = 0.1


def _number(value, name, errors, minimum=None, positive=False):
    """Convert a config value to float, recording an error (and returning None) when it is not a valid number."""
    if isinstance(value, bool):
        errors.append("{} must be a number, got {}".format(name, value))
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append("{} must be a number, got {}".format(name, value))
        return None
    if positive and number <= 0:
        errors.append("{} must be above 0, got {}".format(name, value))
        return None
    if minimum is not None and number < minimum:
        errors.append("{} must be at least {}, got {}".format(name, minimum, value))
        return None
    return number


def parse_two_zone_config(cfg):
    """
    Read and validate the predheat two_zone block.

    Returns (zones, coupling_watts, errors): zones is a list of two dicts (zone1, zone2) holding the numeric
    parameters and the entity ids (None when not given), or None when there are errors. Never raises.
    """
    errors = []
    if not isinstance(cfg, dict):
        return None, 0.0, ["two_zone must be a dictionary"]

    coupling = _number(cfg.get("coupling_watts", 0), "coupling_watts", errors, minimum=0)
    zones = []
    for name in ZONE_NAMES:
        raw = cfg.get(name)
        if not isinstance(raw, dict):
            errors.append("{} is missing".format(name))
            continue
        zone = {"name": name}
        for key in REQUIRED_KEYS:
            if key not in raw:
                errors.append("{}.{} is required".format(name, key))
        zone["heat_loss_watts"] = _number(raw.get("heat_loss_watts", 0), name + ".heat_loss_watts", errors, minimum=0)
        zone["heat_capacity_kwh"] = _number(raw.get("heat_capacity_kwh", 1), name + ".heat_capacity_kwh", errors, positive=True)
        zone["heat_output"] = _number(raw.get("heat_output", 0), name + ".heat_output", errors, minimum=0)
        zone["heat_gain_static"] = _number(raw.get("heat_gain_static", 0), name + ".heat_gain_static", errors)
        zone["hysteresis"] = _number(raw.get("hysteresis", DEFAULT_HYSTERESIS), name + ".hysteresis", errors, minimum=0)
        zone["hysteresis_off"] = _number(raw.get("hysteresis_off", DEFAULT_HYSTERESIS_OFF), name + ".hysteresis_off", errors, minimum=0)
        delta = raw.get("heat_output_delta", None)
        zone["heat_output_delta"] = None if delta is None else _number(delta, name + ".heat_output_delta", errors, positive=True)
        for key in ENTITY_KEYS:
            value = raw.get(key, None)
            if value is not None and not isinstance(value, str):
                errors.append("{}.{} must be an entity id, got {}".format(name, key, value))
                value = None
            zone[key] = value
        zones.append(zone)

    if zones and zones[0]["name"] == "zone1":
        for key in ("internal_temperature", "target_temperature"):
            if not zones[0][key]:
                errors.append("zone1.{} is required".format(key))

    if errors:
        return None, 0.0, errors
    return zones, coupling, []


def simulate_two_zone(zones, coupling_watts, start_temps, start_heating, external, targets, flow_temp, cop, heat_max_power, minutes, step=5):
    """
    Simulate both zones forward in steps of `step` minutes for `minutes` minutes.

    external(minute) is the outside temperature, targets[i](minute) zone i's thermostat target,
    flow_temp(out_temp) the flow temperature and cop(out_temp) the coefficient of performance (electricity =
    heat / cop) for the outside temperature rounded to a whole degree and clamped to -20..20. heat_max_power
    (W, 0 or None for no cap) caps the two zones' total output, scaling both down in proportion.

    Each step applies the thermostats (stop at target + hysteresis, start at target - hysteresis_off, the
    first step included), the emitter outputs, then advances the room temperatures. Returns a dict of lists,
    one entry per step: "minute", "external", "temp" (per zone, after the step), "target", "heating" and
    "heat_watts" (per zone), and "electric_watts".
    """
    temps = [float(start_temps[0]), float(start_temps[1])]
    heating = [bool(start_heating[0]), bool(start_heating[1])]
    hours = step / 60.0
    result = {"minute": [], "external": [], "temp": [[], []], "target": [[], []], "heating": [[], []], "heat_watts": [[], []], "electric_watts": []}

    for minute in range(0, minutes, step):
        ext = external(minute)
        out_temp = min(max(int(ext + 0.5), -20), 20)
        heat = [0.0, 0.0]
        for index, zone in enumerate(zones):
            target = targets[index](minute)
            if heating[index] and temps[index] >= target + zone["hysteresis"]:
                heating[index] = False
            elif not heating[index] and temps[index] <= target - zone["hysteresis_off"]:
                heating[index] = True
            if heating[index]:
                if zone["heat_output_delta"]:
                    heat[index] = zone["heat_output"] * max(flow_temp(out_temp) - temps[index], 0.0) / zone["heat_output_delta"]
                else:
                    heat[index] = zone["heat_output"]
            result["target"][index].append(target)
            result["heating"][index].append(heating[index])

        total = heat[0] + heat[1]
        if heat_max_power and total > heat_max_power:
            scale = heat_max_power / total
            heat = [heat[0] * scale, heat[1] * scale]
            total = heat_max_power
        electric = total / cop(out_temp) if total > 0 else 0.0

        # Heat flowing from zone 2 into zone 1 (negative when zone 1 is the warmer one)
        exchange = coupling_watts * (temps[1] - temps[0])
        flows = (exchange, -exchange)
        for index, zone in enumerate(zones):
            net_watts = heat[index] - zone["heat_loss_watts"] * (temps[index] - ext) + zone["heat_gain_static"] + flows[index]
            temps[index] += net_watts / 1000.0 * hours / zone["heat_capacity_kwh"]
            result["temp"][index].append(temps[index])
            result["heat_watts"][index].append(heat[index])

        result["minute"].append(minute)
        result["external"].append(ext)
        result["electric_watts"].append(electric)
    return result

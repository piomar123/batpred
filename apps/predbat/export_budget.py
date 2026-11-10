# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Export energy budget (export_budget_mode).

Limits battery-to-grid export to energy derived from the household's own PV:

- pv_origin: stored energy is split into a PV part and a grid part. Battery output up to the whole
  on-site load counts as the house's own use and draws the grid part first, even while PV is exported.
  Only battery output beyond the load is export, and only the PV part may be exported.
- pv_equivalent: a bucket filled by PV production and drained by all export, capped (default the
  battery size); battery export is allowed while the bucket has energy.

The step functions are the reference the prediction engines follow. prediction_kernel.cpp mirrors
them inline, so the arithmetic must not change without a kernel parity revision bump.
"""

import math

EXPORT_BUDGET_OFF = 0
EXPORT_BUDGET_PV_ORIGIN = 1
EXPORT_BUDGET_PV_EQUIVALENT = 2
EXPORT_BUDGET_MODE_NAMES = {"off": EXPORT_BUDGET_OFF, "pv_origin": EXPORT_BUDGET_PV_ORIGIN, "pv_equivalent": EXPORT_BUDGET_PV_EQUIVALENT}
EXPORT_BUDGET_REPLAY_DAYS = 7
# A 5 minute meter delta above this (240 kW) is a counter glitch, not energy
EXPORT_BUDGET_MAX_STEP_KWH = 20.0
# The SoC and load meters tick in coarse steps (e.g. 1% of the battery, 0.1 kWh), so in one 5 minute step the battery
# can seem to give more than the house used and in the next less. The replay matches the two across this many minutes,
# carrying at most this much house load the battery seemed to leave unserved (load the meters say neither PV nor the
# grid met), before it counts the rest as a loss
EXPORT_BUDGET_CARRY_MINUTES = 30
EXPORT_BUDGET_CARRY_KWH = 0.25


def parse_export_budget_mode(value):
    """Return the mode constant for an apps.yaml value, or None when it is not a known mode"""
    if value is None or value is False:
        return EXPORT_BUDGET_OFF
    if not isinstance(value, str):
        return None
    return EXPORT_BUDGET_MODE_NAMES.get(value.strip().lower())


def export_budget_house_from_battery(mode, battery_out, load, pv):
    """Battery output (AC) that counts as serving on-site load rather than as export.

    pv_origin counts the whole load, so stored grid energy may run the house while PV is exported;
    pv_equivalent counts PV as covering the load first, so only what PV leaves uncovered.
    """
    if mode == EXPORT_BUDGET_PV_ORIGIN:
        return min(battery_out, max(load, 0.0))
    return min(battery_out, max(load - pv, 0.0))


def export_budget_battery_export_allowed(mode, bucket, pv_part, grid_part, pv_now, pv_direct_export, house_from_battery_ac, stored_to_ac):
    """AC energy the battery may export to the grid in one forced export step.

    pv_equivalent: total export <= bucket + pv_now, so the battery may add what is left after the PV
    exported directly. pv_origin: on-site load is served from the grid part first and export only from
    the PV part that remains; stored_to_ac converts stored kWh to AC at the meter.
    """
    if mode == EXPORT_BUDGET_PV_EQUIVALENT:
        return max(bucket + pv_now - pv_direct_export, 0.0)
    house_stored = house_from_battery_ac / stored_to_ac if stored_to_ac > 0 else 0.0
    pv_left = max(pv_part - max(house_stored - grid_part, 0.0), 0.0)
    return pv_left * stored_to_ac


def export_budget_update(mode, bucket, pv_part, grid_part, soc_before, soc_after, charge_kwh, pv_now, house_frac, export_kwh, cap):
    """Advance the budget state over one step, returns (bucket, pv_part, grid_part).

    charge_kwh is the DC energy drawn into the battery this step (0 when not charging), house_frac the
    share of this step's battery output that served on-site load, export_kwh the energy that reached the
    meter. pv_origin parts are renormalised to soc_after so they always sum to the SoC.
    """
    if mode == EXPORT_BUDGET_PV_EQUIVALENT:
        bucket = min(max(bucket + pv_now - export_kwh, 0.0), cap)
        return bucket, pv_part, grid_part
    delta = soc_after - soc_before
    if delta > 0:
        pv_frac = min(charge_kwh, pv_now) / charge_kwh if charge_kwh > 0 else 0.0
        pv_part += delta * pv_frac
        grid_part += delta * (1.0 - pv_frac)
    elif delta < 0:
        removed = -delta
        removed_house = removed * house_frac
        removed_export = removed - removed_house
        from_grid = min(grid_part, removed_house)
        grid_part -= from_grid
        pv_part = max(pv_part - (removed_house - from_grid), 0.0)
        from_pv = min(pv_part, removed_export)
        pv_part -= from_pv
        grid_part = max(grid_part - (removed_export - from_pv), 0.0)
    total = pv_part + grid_part
    if total > 0:
        scale = soc_after / total
        pv_part *= scale
        grid_part *= scale
    else:
        pv_part = 0.0
        grid_part = soc_after
    return bucket, pv_part, grid_part


def export_budget_floor_percent(soc, exportable, soc_max):
    """Lowest export target (whole percent, rounded up) that cannot sell more than `exportable` stored kWh from `soc`"""
    if soc_max <= 0:
        return 0
    return min(max(int(math.ceil((soc - exportable) * 100.0 / soc_max - 1e-6)), 0), 100)


def _replay_delta(data, ago, step):
    """Energy in the step ending `ago` minutes ago, None for a counter glitch"""
    value = data.get(ago, 0.0) - data.get(ago + step, 0.0)
    if value < -0.05 or value > EXPORT_BUDGET_MAX_STEP_KWH:
        return None
    return max(value, 0.0)


def _replay_soc_delta(soc_history, ago, step):
    """Measured change of stored energy in the step ending `ago` minutes ago, None when unknown or a glitch"""
    if ago not in soc_history or (ago + step) not in soc_history:
        return None
    value = soc_history[ago] - soc_history[ago + step]
    if abs(value) > EXPORT_BUDGET_MAX_STEP_KWH:
        return None
    return value


def export_budget_replay(mode, pv_today, export_today, import_today, load_minutes, soc_max, cap, minutes=EXPORT_BUDGET_REPLAY_DAYS * 24 * 60, step=5, soc_history=None):
    """Replay measured history (cumulative series keyed by minutes ago) through the budget rules.

    Returns (seed, noncompliant_kwh, steps). seed is (bucket, pv_share), or None when there is no usable
    history. noncompliant_kwh is battery export the rules did not allow.

    pv_origin labels battery flow per step. With soc_history (stored kWh keyed by minutes ago) the flow is the
    measured SoC change, starting from the stored energy at the start of the window counted as grid; a drop the
    house and the meter do not explain (losses, BMS resync) comes off the PV part first. Because the meters tick
    in coarse steps, such a drop is first matched against house load the battery seemed to leave unserved within
    EXPORT_BUDGET_CARRY_MINUTES (either side, up to EXPORT_BUDGET_CARRY_KWH of it), and only what is left is a loss.
    Without SoC history the flow is estimated as PV + import - export - load, which reads conversion losses as
    charge, so it is only a fallback.
    Only the PV share of the stored energy is kept; the prediction applies it to the real SoC.
    """
    series = [pv_today, export_today]
    use_soc = False
    if mode == EXPORT_BUDGET_PV_ORIGIN:
        series += [import_today, load_minutes]
        use_soc = bool(soc_history)
        if use_soc:
            series.append(soc_history)
    if any(not data for data in series):
        return None, 0.0, 0
    available = min([minutes] + [max(data.keys()) for data in series])
    available = (available // step) * step
    bucket = 0.0
    pv_part = 0.0
    grid_part = soc_history.get(available, 0.0) if use_soc else 0.0
    noncompliant = 0.0
    steps = 0
    # Coarse meter ticks: house load the battery seemed to leave unserved, and battery output the house and the
    # export meter seemed not to take, each as [minutes ago, kWh] oldest first (see EXPORT_BUDGET_CARRY_MINUTES)
    unserved = []
    unexplained_pending = []

    def take_carry(carry, amount):
        """Remove up to amount kWh from the oldest carry entries, returns how much was taken"""
        taken = 0.0
        while carry and taken < amount:
            use = min(carry[0][1], amount - taken)
            taken += use
            carry[0][1] -= use
            if carry[0][1] <= 1e-9:
                carry.pop(0)
        return taken

    def book_house(parts, kwh):
        """Battery output that served the house: from the grid part first"""
        pv_part, grid_part = parts
        from_grid = min(grid_part, kwh)
        return max(pv_part - (kwh - from_grid), 0.0), grid_part - from_grid

    def book_loss(parts, kwh):
        """Battery output nothing explains (losses, BMS resync): from the PV part first"""
        pv_part, grid_part = parts
        from_pv = min(pv_part, kwh)
        return pv_part - from_pv, max(grid_part - (kwh - from_pv), 0.0)

    for ago in range(available - step, -1, -step):
        if use_soc:
            while unexplained_pending and unexplained_pending[0][0] - ago >= EXPORT_BUDGET_CARRY_MINUTES:
                pv_part, grid_part = book_loss((pv_part, grid_part), unexplained_pending.pop(0)[1])
            while unserved and unserved[0][0] - ago >= EXPORT_BUDGET_CARRY_MINUTES:
                unserved.pop(0)
        pv = _replay_delta(pv_today, ago, step)
        exported = _replay_delta(export_today, ago, step)
        if pv is None or exported is None:
            continue
        if mode == EXPORT_BUDGET_PV_EQUIVALENT:
            noncompliant += max(exported - (bucket + pv), 0.0)
            bucket = min(max(bucket + pv - exported, 0.0), cap)
        else:
            imported = _replay_delta(import_today, ago, step)
            load = _replay_delta(load_minutes, ago, step)
            if imported is None or load is None:
                continue
            if use_soc:
                flow = _replay_soc_delta(soc_history, ago, step)
                if flow is None:
                    continue
            else:
                flow = pv + imported - exported - load
            if flow > 0:
                pv_in = min(flow, pv)
                pv_part += pv_in
                grid_part += flow - pv_in
            elif flow < 0 or (use_soc and load > 0):
                out = -flow if flow < 0 else 0.0
                house = export_budget_house_from_battery(mode, out, load, pv)
                to_grid = out - house
                unexplained = 0.0
                if use_soc:
                    # The meter bounds what can have left for the grid; the rest is loss or a SoC resync
                    unexplained = max(to_grid - exported, 0.0)
                    to_grid -= unexplained
                pv_part, grid_part = book_house((pv_part, grid_part), house)
                from_pv = min(pv_part, to_grid)
                pv_part -= from_pv
                excess = to_grid - from_pv
                noncompliant += excess
                grid_part = max(grid_part - excess, 0.0)
                if use_soc:
                    # Match this step's unexplained output and unserved load against the other side's carry: a match is
                    # the house's own use split across two meter ticks, so it is booked as house use, not a loss
                    matched = take_carry(unserved, unexplained)
                    pv_part, grid_part = book_house((pv_part, grid_part), matched)
                    if unexplained - matched > 1e-9:
                        unexplained_pending.append([ago, unexplained - matched])
                    # Unserved only where the meters say the battery ran the house (load not met by PV or import),
                    # not while it idled and the house imported
                    needed = min(max(load + exported - pv - imported, 0.0), max(load, 0.0))
                    spare = max(needed - house, 0.0)
                    matched = take_carry(unexplained_pending, spare)
                    pv_part, grid_part = book_house((pv_part, grid_part), matched)
                    if spare - matched > 1e-9:
                        unserved.append([ago, spare - matched])
                        while sum(entry[1] for entry in unserved) > EXPORT_BUDGET_CARRY_KWH:
                            take_carry(unserved, sum(entry[1] for entry in unserved) - EXPORT_BUDGET_CARRY_KWH)
            total = pv_part + grid_part
            if soc_max > 0 and total > soc_max:
                pv_part *= soc_max / total
                grid_part *= soc_max / total
        steps += 1
    # Output still waiting for a match when the history ends counts as a loss, so the budget is never overstated
    for _, kwh in unexplained_pending:
        pv_part, grid_part = book_loss((pv_part, grid_part), kwh)
    if steps == 0:
        return None, 0.0, 0
    if mode == EXPORT_BUDGET_PV_EQUIVALENT:
        return (bucket, 0.0), noncompliant, steps
    total = pv_part + grid_part
    return (0.0, pv_part / total if total > 0 else 0.0), noncompliant, steps

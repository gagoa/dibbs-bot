"""Reference data + helpers for NSN/FSC reasoning.

Everything here is pure-Python and side-effect free so it's easy to unit test
and reuse from both the scorer and the dashboard.

This module backs the new 6-subscore framework, which categorizes every FSC
into one of four tiers:

* TIER_A -- "strongly preferred" commercial/industrial parts
            (bearings, hoses, gaskets, filters, fasteners, hardware).
* TIER_B -- "specialty" electrical/mechanical components where competition
            is moderate and margins are decent.
* TIER_C -- "commodity" items (generic fasteners, office supplies) where
            margins are thin and competition is heavy.
* TIER_D -- "avoid": aircraft-critical, weapons, hazardous, medical,
            sole-source electronics, etc.

The legacy ``PREFERRED_FSCS`` and ``RISKY_FSCS`` dicts are still exported for
backward compatibility with the older scoring code and the dashboard's
``_fsc_category`` helper -- they now derive from the tier lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# FSC tiers (the core of the new scoring framework)
# ---------------------------------------------------------------------------

# TIER_A: Strongly favor. Commercial hardware, bearings, hoses, gaskets,
# filters, fasteners with realistic margins, fleet maintenance parts.
TIER_A_FSCS: dict[str, str] = {
    # Bearings
    "3110": "Bearings, Antifriction, Unmounted",
    "3120": "Bearings, Plain, Unmounted",
    "3130": "Bearings, Mounted",
    # Hoses, gaskets, filters
    "4720": "Hose and Tubing, Flexible",
    "4730": "Hose, Pipe, Tube, Fittings, and Couplings",
    "4330": "Filters and Strainers, Fluid",
    "5330": "Packing and Gasket Materials",
    "5331": "O-Rings",
    # Industrial hardware
    "5340": "Hardware, Commercial",
    "5365": "Bushings, Rings, Shims, and Spacers",
    # Specialty fasteners (better margins than commodity 5305/5310)
    "5306": "Bolts",
    "5307": "Studs",
    "5315": "Pins",
    "5320": "Rivets",
    "5325": "Fastening Devices",
    # Fleet maintenance / vehicular components
    "2520": "Vehicular Power Transmission Components",
    "2530": "Vehicular Brake, Steering, Axle, Wheel Components",
    "2540": "Vehicular Furniture and Accessories",
    "2590": "Miscellaneous Vehicular Components",
    "2920": "Engine Electrical System Components, Nonaircraft",
    "2940": "Engine Air and Oil Filters, Nonaircraft",
}

# TIER_B: Specialty electrical/mechanical, narrow but not closed bidder pool.
TIER_B_FSCS: dict[str, str] = {
    "5935": "Connectors, Electrical",
    "5940": "Lugs, Terminals, and Terminal Strips",
    "5945": "Relays and Solenoids",
    "5970": "Electrical Insulators and Insulating Materials",
    "5975": "Electrical Hardware and Supplies",
    "6145": "Wire and Cable, Electrical",
    "6150": "Misc. Electric Power and Distribution Equipment",
    "4710": "Pipe and Tube",
    "4810": "Valves, Powered",
    "4820": "Valves, Nonpowered",
    "5340": "Hardware, Commercial",  # overlaps TIER_A — kept in both, A wins
}

# TIER_C: Generic commodity items. Lots of bidders, thin margins.
TIER_C_FSCS: dict[str, str] = {
    "5305": "Screws",
    "5310": "Nuts and Washers",
    "7510": "Office Supplies",
    "7520": "Office Devices and Accessories",
    "7530": "Stationery and Record Forms",
}

# TIER_D: Avoid or heavily penalize.
TIER_D_FSCS: dict[str, str] = {
    # Aircraft-critical
    "1560": "Airframe Structural Components",
    "1620": "Aircraft Landing Gear Components",
    "1630": "Aircraft Wheel and Brake Systems",
    "1650": "Aircraft Hydraulic Systems",
    "1680": "Aircraft Accessories",
    "2840": "Gas Turbines/Jet Engines, Aircraft",
    "2915": "Aircraft Engine Fuel System Components",
    # Weapons / ordnance
    "1005": "Guns, through 30 mm",
    "1010": "Guns, over 30 mm up to 75 mm",
    "1095": "Miscellaneous Weapons",
    "1340": "Rockets, Rocket Ammunition, and Components",
    "1410": "Guided Missiles",
    # Electronics requiring testing
    "5961": "Semiconductor Devices",
    "5963": "Electronic Modules",
    "5998": "Electrical/Electronic Assemblies",  # complex, often build-to-print
    "5999": "Misc. Electrical and Electronic Components",
    "5995": "Cable, Cord, and Wire Assemblies (Comm/Mil)",
    # Medical
    "6515": "Medical and Surgical Instruments and Supplies",
    "6520": "Dental Instruments and Equipment",
    "6540": "Ophthalmic Instruments and Supplies",
    "6545": "Replenishable Field Medical Sets",
    # Hazardous / chemicals
    "6810": "Chemicals",
    "6820": "Dyes",
    "6830": "Gases: Compressed and Liquefied",
    "6850": "Misc. Chemical Specialties",
    # Specialized containers
    "8145": "Specialized Shipping and Storage Containers",
}


# ---------------------------------------------------------------------------
# Rough order-of-magnitude unit prices in USD by FSC.
#
# These are NOT trying to be accurate — DIBBS unit prices vary enormously
# by item, vendor, and packaging. What we need is a rough multiplier so we
# can multiply by quantity and estimate the order's *capital footprint*
# within an order of magnitude. The capital-efficiency subscore only cares
# about which bucket ($500–$15K vs >$50K) we land in.
#
# Tune these numbers as you accumulate real award data.
# ---------------------------------------------------------------------------
FSC_TYPICAL_UNIT_USD: dict[str, float] = {
    # Tier A: commercial hardware / fasteners (cheap each)
    "5305": 1.0,  "5306": 2.5, "5307": 1.5, "5310": 0.5,
    "5315": 1.5,  "5320": 0.5, "5325": 3.0, "5340": 12.0, "5365": 8.0,
    "5330": 15.0, "5331": 5.0,
    # Tier A: bearings / filters / hoses / gaskets
    "3110": 60.0, "3120": 35.0, "3130": 80.0,
    "4330": 40.0, "4720": 55.0, "4730": 18.0, "2940": 30.0,
    # Tier A: vehicular maintenance parts (medium $)
    "2520": 250.0, "2530": 180.0, "2540": 120.0, "2590": 100.0, "2920": 90.0,
    # Tier B: specialty electrical
    "5935": 25.0, "5940": 8.0, "5945": 18.0, "5970": 12.0, "5975": 15.0,
    "6145": 8.0,  "6150": 50.0,
    # Tier B: pipes/valves
    "4710": 20.0, "4810": 250.0, "4820": 90.0,
    # Tier C: commodity / office
    "7510": 5.0, "7520": 10.0, "7530": 4.0,
    # Tier D: aerospace (expensive each)
    "1560": 1500.0, "1620": 1200.0, "1630": 1500.0,
    "1650": 2500.0, "1680": 2200.0,
    "2840": 5000.0, "2915": 1800.0,
    # Tier D: electronics / assemblies
    "5961": 25.0, "5963": 80.0, "5995": 75.0, "5998": 200.0, "5999": 60.0,
    # Tier D: medical
    "6515": 150.0, "6520": 100.0, "6540": 200.0, "6545": 350.0,
    # Tier D: chemicals / hazardous
    "6810": 60.0, "6820": 80.0, "6830": 120.0, "6850": 90.0,
}

DEFAULT_UNIT_USD: float = 25.0  # used when an FSC isn't in the table


# ---------------------------------------------------------------------------
# Keyword lists used by the keyword-based scoring rules.
# ---------------------------------------------------------------------------

# Words in the item nomenclature that suggest extra scrutiny / risk.
RISKY_KEYWORDS: tuple[str, ...] = (
    # Aviation / aerospace
    "aircraft", "aviation", "airframe", "airworthiness", "faa", "nas",
    "flight critical", "flight-critical",
    # Engines / hydraulics
    "engine", "turbine", "rotor blade", "compressor blade",
    "hydraulic", "actuator", "servovalve",
    # Weapons / ordnance / explosives
    "weapon", "ammunition", "missile", "torpedo", "warhead", "ordnance",
    "explosive", "detonator", "propellant",
    # Hazardous / radiation / medical
    "hazardous", "hazmat", "radioactive", "nuclear", "biohazard",
    "medical", "surgical", "dental",
    # Repair / service / qualification (mostly avoid)
    "repair contract", "overhaul", "remanufacture", "calibration",
    "qualification test", "first article", "qpl", "qml",
    # Safety
    "safety-critical", "safety critical", "mission-critical",
)

# Words that suggest a simple commercial-style part we can quote against.
FRIENDLY_KEYWORDS: tuple[str, ...] = (
    # Fasteners
    "screw", "nut", "washer", "bolt", "rivet", "pin", "stud", "fastener",
    "clip", "anchor",
    # Hardware
    "clamp", "grommet", "bracket", "spacer", "bushing", "shim", "ring",
    # Mechanical
    "bearing", "gasket", "o-ring", "seal", "packing", "hose", "fitting",
    "tube", "valve", "filter", "strainer",
    # Electrical
    "connector", "cable", "wire", "lug", "terminal",
    # Generic
    "hardware", "label", "tag",
)

# Pure-commodity keywords (high competition, low margin).
COMMODITY_KEYWORDS: tuple[str, ...] = (
    "screw", "nut", "washer", "bolt", "rivet", "pin", "stud",
)


# ---------------------------------------------------------------------------
# Legacy preferred/risky dicts (derived from the tier lists so anything that
# still imports them keeps working).
# ---------------------------------------------------------------------------

PREFERRED_FSCS: dict[str, str] = {**TIER_A_FSCS, **TIER_B_FSCS}
RISKY_FSCS: dict[str, str] = dict(TIER_D_FSCS)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FscInfo:
    """Classification for one FSC code."""

    code: str
    label: str
    tier: str        # "A" | "B" | "C" | "D" | "unknown"
    category: str    # "preferred" | "risky" | "unknown"  (legacy)


def classify_fsc(fsc: str | None) -> FscInfo:
    """Return tier + label + legacy category for an FSC.

    A code that appears in multiple tiers (currently only 5340) resolves to
    the most-favorable tier (A beats B).
    """
    if not fsc:
        return FscInfo(code="", label="Unknown", tier="unknown", category="unknown")
    code = str(fsc).strip().zfill(4)

    # Order matters: check A before B because 5340 lives in both.
    for tier, table, legacy in (
        ("A", TIER_A_FSCS, "preferred"),
        ("B", TIER_B_FSCS, "preferred"),
        ("C", TIER_C_FSCS, "unknown"),
        ("D", TIER_D_FSCS, "risky"),
    ):
        if code in table:
            return FscInfo(code=code, label=table[code], tier=tier, category=legacy)

    return FscInfo(code=code, label="Other", tier="unknown", category="unknown")


# Same NSN regex as the parser, kept here too so this module can stand alone.
_NSN_RE = re.compile(r"\b(\d{4})[-\s]?(\d{2})[-\s]?(\d{3})[-\s]?(\d{4})\b")


def is_valid_nsn(value: str | None) -> bool:
    """True if ``value`` looks like a 13-digit NSN."""
    return bool(value and _NSN_RE.search(value))


def fsc_from_nsn(nsn: str | None) -> str | None:
    """Return the 4-digit FSC prefix of an NSN, or None."""
    if not nsn:
        return None
    digits = re.sub(r"\D", "", nsn)
    return digits[:4] if len(digits) >= 4 else None


def has_risky_keyword(item_name: str | None) -> str | None:
    """Return the first risky keyword found in the item name, else None."""
    if not item_name:
        return None
    lower = item_name.lower()
    for kw in RISKY_KEYWORDS:
        if kw in lower:
            return kw
    return None


def has_friendly_keyword(item_name: str | None) -> str | None:
    """Return the first 'friendly' keyword found, else None."""
    if not item_name:
        return None
    lower = item_name.lower()
    for kw in FRIENDLY_KEYWORDS:
        if kw in lower:
            return kw
    return None


def has_commodity_keyword(item_name: str | None) -> str | None:
    """Return the first commodity keyword found, else None."""
    if not item_name:
        return None
    lower = item_name.lower()
    for kw in COMMODITY_KEYWORDS:
        if kw in lower:
            return kw
    return None


def estimate_unit_price(fsc: str | None, item_name: str | None = None) -> float:
    """Best-effort order-of-magnitude unit price in USD."""
    if fsc:
        code = str(fsc).strip().zfill(4)
        if code in FSC_TYPICAL_UNIT_USD:
            return FSC_TYPICAL_UNIT_USD[code]
    return DEFAULT_UNIT_USD

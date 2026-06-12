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
# FSC tiers (the core of the scoring framework)
#
# The universe is intentionally broad: ANY item a small distributor could
# realistically source. Tiers describe the *expected ease of profitable
# sourcing*, not "preference for fasteners". Per the calibration spec, we
# do NOT preferentially favor screws/bolts/nuts/washers -- they're commodity.
# ---------------------------------------------------------------------------

# TIER_A: Specialty industrial items distributors can source quickly with
# decent margin -- bearings, seals, gaskets, hoses, filters, fittings, etc.
TIER_A_FSCS: dict[str, str] = {
    # Bearings
    "3110": "Bearings, Antifriction, Unmounted",
    "3120": "Bearings, Plain, Unmounted",
    "3130": "Bearings, Mounted",
    # Hoses, gaskets, seals, o-rings
    "4720": "Hose and Tubing, Flexible",
    "4730": "Hose, Pipe, Tube, Fittings, and Couplings",
    "5330": "Packing and Gasket Materials",
    "5331": "O-Rings",
    # Filters & strainers
    "4330": "Filters and Strainers, Fluid",
    "2940": "Engine Air and Oil Filters, Nonaircraft",
    # Springs / mechanical
    "5360": "Coils, Springs, and Leaves",
    # Industrial hardware (NOT commodity screws/nuts)
    "5340": "Hardware, Commercial",
    "5342": "Hardware, Weapon System",
    "5365": "Bushings, Rings, Shims, and Spacers",
}

# TIER_B: Sourceable from specialty distributors -- broad coverage of
# industrial electrical, hydraulics, pneumatics, fleet, tools, packaging,
# and even sensible aerospace hardware items (penalties for critical
# applications happen separately via risky keywords).
TIER_B_FSCS: dict[str, str] = {
    # Industrial electrical / electronic parts
    "5925": "Circuit Breakers",
    "5930": "Switches",
    "5935": "Connectors, Electrical",
    "5940": "Lugs, Terminals, and Terminal Strips",
    "5945": "Relays and Solenoids",
    "5950": "Coils and Transformers",
    "5955": "Oscillators and Piezoelectric Crystals",
    "5970": "Electrical Insulators and Insulating Materials",
    "5975": "Electrical Hardware and Supplies",
    "5985": "Antennas, Waveguides, and Related Equipment",
    "5990": "Synchros and Resolvers",
    "6105": "Motors, Electrical",
    "6110": "Electrical Control Equipment",
    "6115": "Generators and Generator Sets, Electrical",
    "6135": "Batteries, Nonrechargeable",
    "6140": "Batteries, Rechargeable",
    "6145": "Wire and Cable, Electrical",
    "6150": "Misc. Electric Power and Distribution Equipment",
    # Hydraulics & pneumatics
    "4810": "Valves, Powered",
    "4820": "Valves, Nonpowered",
    "4710": "Pipe and Tube",
    # Fleet maintenance / vehicular
    "2520": "Vehicular Power Transmission Components",
    "2530": "Vehicular Brake, Steering, Axle, Wheel Components",
    "2540": "Vehicular Furniture and Accessories",
    "2590": "Miscellaneous Vehicular Components",
    "2910": "Engine Fuel System Components, Nonaircraft",
    "2920": "Engine Electrical System Components, Nonaircraft",
    "2930": "Engine Cooling System Components, Nonaircraft",
    # Mechanical power transmission
    "3010": "Torque Converters and Speed Changers",
    "3020": "Gears, Pulleys, Sprockets, and Transmission Chain",
    "3030": "Belting, Drive Belts, Fan Belts, and Accessories",
    "3040": "Misc. Power Transmission Equipment",
    # Tools / instruments (industrial consumables and tooling)
    "5110": "Hand Tools, Edged, Nonpowered",
    "5120": "Hand Tools, Nonedged, Nonpowered",
    "5130": "Hand Tools, Power-Driven",
    "5180": "Sets, Kits, and Outfits of Hand Tools",
    "5210": "Measuring Tools, Craftsmen's",
    "5220": "Inspection Gages and Precision Layout Tools",
    "5280": "Sets, Kits, and Outfits of Measuring Tools",
    # Plumbing, heating, A/C, refrigeration (commercial)
    "4130": "Refrigeration and Air-Conditioning Components",
    "4520": "Space Heaters and Domestic Boilers",
    # Safety equipment
    "4240": "Safety and Rescue Equipment",
    # Packaging materials
    "8125": "Bottles and Jars",
    "8135": "Packaging and Packing Bulk Materials",
    # Maintenance and repair shop supplies
    "7920": "Brooms, Brushes, Mops, and Sponges",
    "7930": "Cleaning and Polishing Compounds and Preparations",
    "9150": "Oils and Greases: Cutting, Lubricating, and Hydraulic",
    # Sensible aerospace hardware (penalized further if risky-keyword hit)
    "1560": "Airframe Structural Components",
    "1620": "Aircraft Landing Gear Components",
    "1630": "Aircraft Wheel and Brake Systems",
    "1650": "Aircraft Hydraulic Systems",
    "1660": "Aircraft Air Conditioning, Heating, and Pressurizing",
    "1680": "Aircraft Accessories",
    "1730": "Aircraft Ground Servicing Equipment",
    # Misc industrial / commercial
    "3510": "Laundry and Dry Cleaning Equipment",
    "3540": "Wrapping and Packaging Machinery",
    "5998": "Electrical/Electronic Assemblies",  # often quotable with right CAGE
    "6210": "Indoor and Outdoor Electric Lighting Fixtures",
    "6220": "Electric Vehicular Lights and Fixtures",
    "6230": "Electric Portable and Hand Lighting Equipment",
    # More industrial / commercial broadly sourceable categories
    "4140": "Fans, Air Circulators, and Blower Equipment",
    "4210": "Fire Fighting Equipment",
    "4310": "Compressors and Vacuum Pumps",
    "4320": "Power and Hand Pumps",
    "4410": "Industrial Boilers",
    "4420": "Heat Exchangers and Steam Condensers",
    "4440": "Driers, Dehydrators, and Anhydrators",
    "4460": "Air Purification Equipment",
    "4510": "Plumbing Fixtures and Accessories",
    "4540": "Misc. Plumbing, Heating, and Sanitation Equipment",
    "4910": "Motor Vehicle Maintenance and Repair Shop Equipment",
    "4920": "Aircraft Maintenance and Repair Shop Equipment",
    "4925": "Ammunition Maintenance Equipment",
    "4930": "Lubrication and Fuel Dispensing Equipment",
    "3950": "Material Handling Equipment",
    "3990": "Pallets, Containers, Drums for Storage",
    "2040": "Hull and Marine Hardware",
    "2090": "Misc Ship and Marine Equipment",
    "6105": "Motors, Electrical",  # duplicate-safe
    "6130": "Converters, Electrical, Nonrotating",
    "6605": "Navigational Instruments",
    "6625": "Electrical and Electronic Test Instruments",
    "6630": "Chemical Analysis Instruments",
    "6635": "Physical Properties Testing Equipment",
    "6640": "Laboratory Equipment and Supplies",
    "6680": "Liquid/Gas Flow, Level, Motion Measuring Instruments",
    "6685": "Pressure, Temperature, Humidity Measuring Instruments",
    "6695": "Combination and Misc Instruments",
    "7025": "ADP I/O and Storage Devices",
    "7050": "ADP Components",
    "7195": "Misc Furniture and Fixtures",
    "7220": "Floor Coverings",
    "7240": "Household and Commercial Utility Containers",
    "7290": "Misc Household and Commercial Furnishings",
    "7330": "Kitchen Equipment and Appliances",
    "7350": "Tableware",
    "7690": "Misc Printed Matter and Labels",
    "7920": "Brooms, Brushes, Mops, Sponges (duplicate-safe)",
    "8010": "Paints, Dopes, Varnishes, Related",
    "8030": "Preservative and Sealing Compounds",
    "8040": "Adhesives",
    "8105": "Bags and Sacks",
    "8110": "Drums and Cans",
    "8115": "Boxes, Cartons, and Crates",
    "9320": "Rubber Fabricated Materials",
    "9330": "Plastics Fabricated Materials",
    "9505": "Wire, Nonelectrical, Iron and Steel",
    "9510": "Bars and Rods, Iron and Steel",
}

# TIER_C: Commodity items. Quotable but lots of competition, thin margins.
# Per the spec, fasteners belong here (no preference vs other items).
TIER_C_FSCS: dict[str, str] = {
    "5305": "Screws",
    "5306": "Bolts",
    "5307": "Studs",
    "5310": "Nuts and Washers",
    "5315": "Pins",
    "5320": "Rivets",
    "5325": "Fastening Devices",
    "7510": "Office Supplies",
    "7520": "Office Devices and Accessories",
    "7530": "Stationery and Record Forms",
}

# TIER_D: Hard to source / poor fit for a small distributor. Narrowed to
# truly difficult: weapons, hazmat, custom semiconductors, military cable
# assemblies, complex medical, specialized containers.
TIER_D_FSCS: dict[str, str] = {
    # Weapons / ordnance
    "1005": "Guns, through 30 mm",
    "1010": "Guns, over 30 mm up to 75 mm",
    "1015": "Guns, 75 mm through 125 mm",
    "1095": "Miscellaneous Weapons",
    "1340": "Rockets, Rocket Ammunition, and Components",
    "1410": "Guided Missiles",
    "1377": "Pyrotechnics",
    # True aerospace critical (engines, jet fuel systems) -- distinct from
    # Tier-B aerospace hardware. These FSCs are dominated by OEMs.
    "2840": "Gas Turbines/Jet Engines, Aircraft",
    "2915": "Aircraft Engine Fuel System Components",
    # Custom semiconductors / military assemblies
    "5961": "Semiconductor Devices",
    "5963": "Electronic Modules",
    "5995": "Cable, Cord, and Wire Assemblies (Comm/Mil)",
    "5999": "Misc. Electrical and Electronic Components",
    # Complex medical / dental
    "6505": "Drugs and Biologicals",
    "6510": "Surgical Dressing Materials",
    "6515": "Medical and Surgical Instruments and Supplies",
    "6520": "Dental Instruments and Equipment",
    "6525": "X-Ray Equipment and Supplies",
    "6530": "Hospital Furniture, Equipment, Utensils",
    "6540": "Ophthalmic Instruments and Supplies",
    "6545": "Replenishable Field Medical Sets",
    "6550": "In Vitro Diagnostic Substances",
    # Hazardous / chemicals
    "6810": "Chemicals",
    "6820": "Dyes",
    "6830": "Gases: Compressed and Liquefied",
    "6850": "Misc. Chemical Specialties",
    # Specialized shipping containers (often custom-built)
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
    # Tier A: specialty industrial (decent margin, distributor-friendly)
    "3110": 60.0, "3120": 35.0, "3130": 80.0,
    "4330": 40.0, "4720": 55.0, "4730": 18.0, "2940": 30.0,
    "5330": 15.0, "5331": 5.0, "5340": 12.0, "5365": 8.0,
    # Tier B: industrial electrical
    "5925": 35.0, "5930": 20.0, "5935": 25.0, "5940": 8.0,
    "5945": 18.0, "5950": 40.0, "5955": 30.0, "5970": 12.0,
    "5975": 15.0, "5985": 60.0, "5990": 90.0,
    "6105": 180.0, "6110": 250.0, "6115": 600.0, "6135": 20.0,
    "6140": 40.0, "6145": 8.0, "6150": 50.0,
    # Tier B: hydraulics / pneumatics / pipes / valves
    "4710": 20.0, "4810": 250.0, "4820": 90.0,
    # Tier B: fleet maintenance
    "2520": 250.0, "2530": 180.0, "2540": 120.0, "2590": 100.0,
    "2910": 80.0, "2920": 90.0, "2930": 70.0,
    # Tier B: mechanical power transmission
    "3010": 200.0, "3020": 90.0, "3030": 25.0, "3040": 60.0,
    # Tier B: tools and instruments
    "5110": 20.0, "5120": 25.0, "5130": 80.0, "5180": 250.0,
    "5210": 60.0, "5220": 90.0, "5280": 300.0,
    # Tier B: HVAC / safety / packaging / shop supplies
    "4130": 80.0, "4520": 250.0, "4240": 75.0,
    "8125": 4.0, "8135": 3.0,
    "7920": 8.0, "7930": 15.0, "9150": 25.0,
    # Tier B: aerospace hardware (these get penalized further if risky-keyword hit)
    "1560": 800.0, "1620": 800.0, "1630": 900.0, "1650": 1500.0,
    "1660": 600.0, "1680": 1200.0, "1730": 1500.0,
    # Tier B: misc industrial / commercial
    "3510": 350.0, "3540": 500.0, "5998": 200.0,
    "6210": 60.0, "6220": 40.0, "6230": 30.0,
    # Tier C: commodity fasteners (cheap each, no preference)
    "5305": 1.0,  "5306": 2.5, "5307": 1.5, "5310": 0.5,
    "5315": 1.5,  "5320": 0.5, "5325": 3.0,
    # Tier C: office
    "7510": 5.0, "7520": 10.0, "7530": 4.0,
    # Tier D: weapons / ordnance (typically high unit cost)
    "1005": 1500.0, "1010": 2500.0, "1015": 4000.0,
    "1095": 800.0, "1340": 2500.0, "1410": 25000.0, "1377": 80.0,
    # Tier D: true aerospace critical
    "2840": 5000.0, "2915": 1800.0,
    # Tier D: military electronics / assemblies
    "5961": 25.0, "5963": 80.0, "5995": 75.0, "5999": 60.0,
    # Tier D: medical
    "6515": 150.0, "6520": 100.0, "6540": 200.0, "6545": 350.0,
    # Tier D: chemicals / hazmat
    "6810": 60.0, "6820": 80.0, "6830": 120.0, "6850": 90.0,
    # Tier D: specialized containers
    "8145": 600.0,
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

# Keywords that signal an item is likely custom-manufactured or requires
# significant engineering work. Per the calibration spec these are hard
# exclusions for a small distributor, not just penalty triggers.
CUSTOM_OR_ENGINEERED_KEYWORDS: tuple[str, ...] = (
    "custom", "to print", "build to print", "engineered to",
    "made to order", "manufactured to", "manufacturer's drawing",
    "fabricated", "machined to", "assembly per drawing",
    "non-standard",
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

    A code that appears in multiple tiers (currently only 5340 and 6105
    overlap A and B) resolves to the most-favorable tier (A beats B).

    Per the scoring spec the universe is BROAD: any item a small distributor
    could realistically source. So unrecognized FSCs default to Tier B
    behavior ("specialty / sourceable"), not penalized. The "unknown" tier
    only triggers when the FSC field itself is missing.
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

    # Unrecognized FSC -> treat as Tier B by default (broad universe).
    return FscInfo(code=code, label="Other (assumed sourceable)", tier="B", category="preferred")


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


def is_likely_custom_or_engineered(item_name: str | None, raw_text: str | None = None) -> str | None:
    """Return the first custom/engineered keyword if the item looks custom-made.

    Searches both the short item name and (optionally) the first chunk of
    raw_text. Per the spec, items matching these keywords should be excluded
    from a small distributor's universe entirely (capped at AVOID).
    """
    haystack = (item_name or "").lower()
    if raw_text:
        haystack = haystack + " " + raw_text[:3000].lower()
    if not haystack:
        return None
    for kw in CUSTOM_OR_ENGINEERED_KEYWORDS:
        if kw in haystack:
            return kw
    return None


def estimate_unit_price(fsc: str | None, item_name: str | None = None) -> float:
    """Best-effort order-of-magnitude unit price in USD."""
    if fsc:
        code = str(fsc).strip().zfill(4)
        if code in FSC_TYPICAL_UNIT_USD:
            return FSC_TYPICAL_UNIT_USD[code]
    return DEFAULT_UNIT_USD

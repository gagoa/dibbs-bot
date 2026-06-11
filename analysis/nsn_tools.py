"""Reference data + helpers for NSN/FSC reasoning.

Everything here is pure-Python and side-effect free so it's easy to unit test
and reuse from both the scorer and the dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Curated FSC lists. These reflect the user's stated preferences and are easy
# to tweak as you learn what wins are repeatable vs. painful.
# ---------------------------------------------------------------------------

# FSCs that tend to be simple hardware / commercial parts -> beginner friendly.
PREFERRED_FSCS: dict[str, str] = {
    "5305": "Screws",
    "5310": "Nuts and Washers",
    "5325": "Fastening Devices",
    "5340": "Hardware, Commercial",
    "5935": "Connectors, Electrical",
    "5975": "Electrical Hardware",
    "5998": "Electrical/Electronic Assemblies",
    "6145": "Wire and Cable",
    "6150": "Cable Assemblies",
}

# FSCs that signal aviation-critical, weapons, or otherwise high-complexity
# items. We don't refuse them, just penalize and flag.
RISKY_FSCS: dict[str, str] = {
    "1560": "Airframe Structural Components",
    "1650": "Aircraft Hydraulic Systems",
    "1680": "Aircraft Accessories",
    "2840": "Gas Turbines / Jet Engines",
    "2915": "Aircraft Engine Fuel System Components",
    "5995": "Cable Assemblies (military/aviation)",
    "8145": "Specialized Shipping Containers",
}

# Words in the item nomenclature that suggest extra scrutiny / risk.
RISKY_KEYWORDS: tuple[str, ...] = (
    "aircraft", "aviation", "airframe", "engine", "turbine",
    "weapon", "ammunition", "missile", "torpedo", "warhead",
    "hydraulic", "fuel", "actuator", "safety-critical",
    "explosive", "ordnance", "nuclear", "radiation",
)

# Words that suggest a simple commercial-style part.
FRIENDLY_KEYWORDS: tuple[str, ...] = (
    "screw", "nut", "washer", "bolt", "clamp", "grommet",
    "bracket", "spacer", "fastener", "connector", "cable",
    "wire", "hardware", "label", "tag",
)


@dataclass(frozen=True)
class FscInfo:
    """Classification for one FSC code."""

    code: str
    label: str
    category: str  # "preferred" | "risky" | "unknown"


def classify_fsc(fsc: str | None) -> FscInfo:
    """Bucket an FSC into preferred / risky / unknown."""
    if not fsc:
        return FscInfo(code="", label="Unknown", category="unknown")
    code = str(fsc).strip().zfill(4)
    if code in PREFERRED_FSCS:
        return FscInfo(code=code, label=PREFERRED_FSCS[code], category="preferred")
    if code in RISKY_FSCS:
        return FscInfo(code=code, label=RISKY_FSCS[code], category="risky")
    return FscInfo(code=code, label="Other", category="unknown")


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

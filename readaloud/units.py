"""Speak unit abbreviations as words ("280 mm" -> "280 millimeters").

espeak-ng, which Piper uses for pronunciation, spells most unit symbols
out letter by letter ("mm" -> "em em", "Hz" -> "aitch zee"). This rewrites
a word's spoken text only; the displayed text is untouched.

Many symbols are also ordinary words or letters ("s", "A", "K", "m", "pc",
"mag"), so those are expanded only right after a number. Unambiguous ones
("mm", "nm", "Hz", "arcsec") are expanded anywhere, which also covers table
headers like "(mm)".
"""
from __future__ import annotations

import re

# symbol: (singular, plural, expand only after a number?)
_UNITS: dict[str, tuple[str, str, bool]] = {}


def _add(symbols: str, singular: str, plural: str | None = None, after_number: bool = False) -> None:
    for symbol in symbols.split():
        _UNITS[symbol] = (singular, plural or singular + "s", after_number)


MICRO = ("µ", "μ")  # micro sign and Greek mu are both used for micro-


def _add_micro(stem: str, singular: str, plural: str | None = None, after_number: bool = False) -> None:
    _add(" ".join(m + stem for m in MICRO), singular, plural, after_number)


# Length
_add("km", "kilometer")
_add("cm", "centimeter")
_add("mm", "millimeter")
_add_micro("m", "micrometer")
_add("um", "micrometer", after_number=True)
_add("nm", "nanometer")
_add("pm", "picometer", after_number=True)
_add("Å", "angstrom")
_add("m", "meter", after_number=True)
_add("ft", "foot", "feet", after_number=True)
_add("au AU", "astronomical unit", after_number=True)
_add("pc", "parsec", after_number=True)
_add("kpc", "kiloparsec")
_add("Mpc", "megaparsec")
_add("Gpc", "gigaparsec")
_add("ly", "light-year", after_number=True)
# Time
_add("s sec", "second", after_number=True)
_add("ms", "millisecond", after_number=True)
_add_micro("s", "microsecond")
_add("ns", "nanosecond", after_number=True)
_add("min", "minute", after_number=True)
_add("h hr", "hour", after_number=True)
_add("yr", "year", after_number=True)
_add("Myr", "million years", "million years")
_add("Gyr", "billion years", "billion years")
# Frequency
_add("Hz", "hertz", "hertz")
_add("kHz", "kilohertz", "kilohertz")
_add("MHz", "megahertz", "megahertz")
_add("GHz", "gigahertz", "gigahertz")
_add("THz", "terahertz", "terahertz")
_add("rpm", "revolution per minute", "revolutions per minute")
# Angle
_add("deg", "degree", after_number=True)
_add("rad", "radian", after_number=True)
_add("mrad", "milliradian")
_add_micro("rad", "microradian")
_add("arcsec", "arcsecond")
_add("arcmin", "arcminute")
_add("mas", "milliarcsecond", after_number=True)
_add("sr", "steradian", after_number=True)
# Mass
_add("kg", "kilogram")
_add("g", "gram", after_number=True)
_add("mg", "milligram")
_add_micro("g", "microgram")
# Temperature
_add("K", "kelvin", "kelvin", after_number=True)
_add("mK", "millikelvin", "millikelvin")
_add("°C", "degree Celsius", "degrees Celsius")
_add("°F", "degree Fahrenheit", "degrees Fahrenheit")
# Electrical, power, energy
_add("V", "volt", after_number=True)
_add("mV", "millivolt")
_add("kV", "kilovolt")
_add("A", "ampere", after_number=True)
_add("mA", "milliampere")
_add_micro("A", "microampere")
_add("W", "watt", after_number=True)
_add("mW", "milliwatt")
_add("kW", "kilowatt")
_add("Ω", "ohm", after_number=True)
_add("kΩ", "kilohm")
_add("MΩ", "megohm")
_add("J", "joule", after_number=True)
_add("eV", "electronvolt")
_add("keV", "kiloelectronvolt")
_add("MeV", "megaelectronvolt")
_add("GeV", "gigaelectronvolt")
_add("erg", "erg", after_number=True)
_add("N", "newton", after_number=True)
# Pressure
_add("Pa", "pascal", after_number=True)
_add("hPa", "hectopascal")
_add("kPa", "kilopascal")
_add("bar", "bar", after_number=True)
_add("mbar", "millibar")
_add("Torr", "torr", "torr")
_add("psi", "pound per square inch", "pounds per square inch", after_number=True)
# Other
_add("L", "liter", after_number=True)
_add("mL", "milliliter", after_number=True)
_add("dB", "decibel")
_add("px", "pixel", after_number=True)
_add("mag", "magnitude", after_number=True)
_add("Jy", "jansky", after_number=True)
_add("mJy", "millijansky", "millijanskys")
_add_micro("Jy", "microjansky", "microjanskys")
_add("ppm", "part per million", "parts per million")
_add("ppb", "part per billion", "parts per billion")

_POWERS = {"²": "square", "^2": "square", "³": "cubic", "^3": "cubic"}
_INVERSE = ("⁻¹", "^-1", "^−1")

_LEADING = re.compile(r"^[(\[{\"'“‘]*")
_TRAILING = re.compile(r"[)\]}\"'”’.,;:!?]*$")
_NUMBER = r"[~≈<>≤≥±+\-−]?\d[\d,]*(?:\.\d+)?"
_NUMBER_TOKEN = re.compile(rf"^{_NUMBER}(?:[–\-−]{_NUMBER})?$")
_FUSED = re.compile(rf"^({_NUMBER})(\D.*)$")


def _strip(token: str) -> tuple[str, str, str]:
    lead = _LEADING.match(token).group()
    trail = _TRAILING.search(token[len(lead):]).group()
    return lead, token[len(lead) : len(token) - len(trail)], trail


def _is_one(number: str) -> bool:
    return number.lstrip("~≈<>≤≥±+") == "1"


def _unit_words(core: str, after_number: bool, singular: bool) -> str | None:
    """Words for a unit expression (no punctuation), or None if it isn't one."""
    if "/" in core:
        parts = core.split("/")
        if len(parts) != 2 or not all(parts):
            return None
        # Without a number before it, at least one side must be unambiguous,
        # so "N/A" in a table isn't read as "newtons per ampere".
        top = _unit_words(parts[0], after_number, singular)
        bottom = _unit_words(parts[1], after_number, True)
        if not (after_number or top or bottom):
            return None
        top = top or _unit_words(parts[0], True, singular)
        bottom = bottom or _unit_words(parts[1], True, True)
        return f"{top} per {bottom}" if top and bottom else None
    for suffix in _INVERSE:
        if core.endswith(suffix):
            unit = _unit_words(core[: -len(suffix)], True, True)
            return f"per {unit}" if unit else None
    for suffix, power in _POWERS.items():
        if core.endswith(suffix):
            unit = _unit_words(core[: -len(suffix)], True, singular)
            return f"{power} {unit}" if unit else None
    entry = _UNITS.get(core)
    if entry is None or (entry[2] and not after_number):
        return None
    return entry[0] if singular else entry[1]


def expand_units(token: str, previous: str | None) -> str | None:
    """Spoken form of `token` with its unit written out, or None if it has
    no unit to expand. `previous` is the preceding word's text (a number
    there makes ambiguous symbols count as units)."""
    lead, core, trail = _strip(token)
    if not core:
        return None
    fused = _FUSED.match(core)
    if fused:
        number, unit = fused.groups()
        # A number run into a single ambiguous letter is usually an
        # identifier ("130942N", "4K", "1990s"), not a quantity.
        entry = _UNITS.get(unit)
        if entry and entry[2] and len(unit) == 1:
            return None
        words = _unit_words(unit, True, _is_one(number))
        return f"{lead}{number} {words}{trail}" if words else None
    prev_lead, prev_core, prev_trail = _strip(previous) if previous else ("", "", "")
    # "Fig. 2. A blue..." - a number that ends a sentence or clause doesn't
    # make the next word a unit.
    after_number = bool(_NUMBER_TOKEN.match(prev_core)) and not prev_trail
    words = _unit_words(core, after_number, after_number and _is_one(prev_core))
    return f"{lead}{words}{trail}" if words else None

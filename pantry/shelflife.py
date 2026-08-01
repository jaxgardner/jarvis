"""How long food lasts, by category and storage location.

A checked-in table, not a model call. The design decision this file embodies:
the model reads pixels and maps `GV WHL MLK 1GAL` to the category `milk`; this
table turns that category plus a purchase date into a *default* expiry, which
a human then confirms. Nothing here is authoritative — it exists to save you
typing the boring dates, and every value it produces is editable in the review
screen before it reaches the inventory.

So when milk is consistently wrong, you edit one line here and every future
gallon is fixed. That is the whole reason the dates do not come from the
model: a prompt cannot be corrected once and for all.

Values are conservative — "still good" rather than "technically safe" — since
the cost of an early nudge is a glance at the fridge, and the cost of a late
one is throwing food away.

`None` means nothing expires it on any useful horizon. It is not "unknown with
a shrug"; it is a real answer, and the review screen renders it as a blank
date rather than inventing one.
"""

import re
from datetime import date, timedelta

# category -> {location: days}. A category may list only the locations that
# make sense for it; `days_for` falls back to the pantry figure when the
# requested location is absent, and to None when neither exists.
_TABLE: dict[str, dict[str, int | None]] = {
    # ── dairy ──
    "milk": {"fridge": 7, "freezer": 90},
    "cream": {"fridge": 10},
    "yogurt": {"fridge": 14},
    "cheese_hard": {"fridge": 42, "freezer": 180},
    "cheese_soft": {"fridge": 10},
    "butter": {"fridge": 60, "freezer": 270},
    "eggs": {"fridge": 28},
    # ── meat and fish ──
    "chicken": {"fridge": 2, "freezer": 270},
    "beef": {"fridge": 4, "freezer": 270},
    "pork": {"fridge": 4, "freezer": 180},
    "ground_meat": {"fridge": 2, "freezer": 120},
    "fish": {"fridge": 2, "freezer": 180},
    "shellfish": {"fridge": 2, "freezer": 90},
    "bacon": {"fridge": 7, "freezer": 30},
    "deli_meat": {"fridge": 5, "freezer": 60},
    # ── produce ──
    "leafy_greens": {"fridge": 5},
    "spinach": {"fridge": 5},
    "lettuce": {"fridge": 7},
    "berries": {"fridge": 4, "freezer": 300},
    "apples": {"fridge": 30, "pantry": 14},
    "citrus": {"fridge": 21, "pantry": 10},
    "bananas": {"pantry": 5},
    "stone_fruit": {"fridge": 7, "pantry": 4},
    "tomatoes": {"pantry": 7, "fridge": 10},
    "peppers": {"fridge": 10},
    "broccoli": {"fridge": 7},
    "carrots": {"fridge": 21},
    "celery": {"fridge": 14},
    "mushrooms": {"fridge": 7},
    "onions": {"pantry": 45},
    "garlic": {"pantry": 90},
    "potatoes": {"pantry": 45},
    "avocado": {"pantry": 4, "fridge": 7},
    "herbs_fresh": {"fridge": 7},
    # ── bakery ──
    "bread": {"pantry": 5, "fridge": 14, "freezer": 90},
    "tortillas": {"fridge": 30, "pantry": 7},
    "bakery_sweet": {"pantry": 4},
    # ── prepared and leftovers ──
    "leftovers": {"fridge": 4, "freezer": 90},
    "hummus": {"fridge": 7},
    "salsa": {"fridge": 14},
    "juice": {"fridge": 10},
    "tofu": {"fridge": 7},
    # ── frozen ──
    "frozen_meal": {"freezer": 300},
    "frozen_vegetables": {"freezer": 300},
    "ice_cream": {"freezer": 120},
    # ── shelf stable: real answers, deliberately None ──
    "pasta_dry": {"pantry": None},
    "rice": {"pantry": None},
    "flour": {"pantry": 365},
    "sugar": {"pantry": None},
    "salt": {"pantry": None},
    "spices": {"pantry": 730},
    "oil": {"pantry": 540},
    "vinegar": {"pantry": None},
    "canned": {"pantry": 730},
    "cereal": {"pantry": 180},
    "snacks": {"pantry": 90},
    "coffee": {"pantry": 180},
    "tea": {"pantry": 540},
    "condiments": {"fridge": 180, "pantry": 180},
    "beverages": {"pantry": 270},
    # ── not food ──
    "household": {"pantry": None},
    "personal_care": {"pantry": None},
    "other": {"pantry": None},
}

# Where a thing goes if the extractor does not say. Keyed by the same
# categories, and `test_every_category_has_a_default_location` keeps the two
# dicts from drifting apart.
DEFAULT_LOCATION: dict[str, str] = {
    category: (
        "freezer"
        if set(locations) == {"freezer"}
        else "fridge"
        if "fridge" in locations
        else "pantry"
    )
    for category, locations in _TABLE.items()
}

# Interpolated into the extraction tool schema as an enum, so it must be
# byte-stable across runs — hence sorted.
CATEGORIES: list[str] = sorted(_TABLE)

LOCATIONS = ("fridge", "freezer", "pantry")


def days_for(category: str | None, location: str) -> int | None:
    """Shelf life in days, or None when nothing expires it on a useful horizon."""
    if not category:
        return None
    entry = _TABLE.get(category)
    if entry is None:
        return None
    if location in entry:
        return entry[location]
    # An unlisted location falls back to the pantry figure rather than to
    # None, so "canned beans in the fridge" still gets a sane default.
    return entry.get("pantry")


# Words that name a category the table spells differently. Deliberately tiny:
# this is a lookup table, not a synonym engine, and anything it misses costs a
# blank date in a screen you are already reviewing.
_SYNONYMS = {
    "yoghurt": "yogurt",
    "hamburger": "ground_meat",
    "mince": "ground_meat",
    "prawns": "shellfish",
    "shrimp": "shellfish",
    "salmon": "fish",
    "tuna": "fish",
    "turkey": "chicken",
    "scallions": "onions",
    "pasta": "pasta_dry",
    "noodles": "pasta_dry",
    "soda": "beverages",
}


def guess_category(name: str | None) -> str | None:
    """Best category for a typed item name, or None.

    For manual entry, where a person types "whole milk" rather than a model
    reading `GV WHL MLK 1GAL` off a receipt. Pure string work on purpose: the
    input is already text, so there is nothing to OCR, and keeping a model out
    of it means manual entry costs nothing and cannot be slow.

    Matched by *word*, never by substring. "tea" is a substring of "steak",
    and filing a steak under tea would hand it a two-year shelf life — the
    kind of wrong that is worse than no answer at all.

    None is a real answer meaning "no date", which the review screen renders
    as a blank for you to fill in.
    """
    if not name:
        return None
    words = set(re.findall(r"[a-z]+", name.lower()))
    if not words:
        return None

    for word in words:
        if word in _SYNONYMS:
            return _SYNONYMS[word]

    # Longest match wins, so "hard cheese" beats a bare "cheese" would-be
    # match and lands on cheese_hard rather than something shorter.
    best: str | None = None
    for category in CATEGORIES:
        parts = set(category.split("_"))
        if parts <= words and (best is None or len(parts) > len(best.split("_"))):
            best = category
    return best


def expires_on(category: str | None, location: str, purchased_on: str) -> str | None:
    """`purchased_on` + the shelf life, as YYYY-MM-DD.

    A malformed purchase date yields None rather than raising: extraction can
    misread a crumpled receipt, and that should cost one blank field in the
    review screen, not a 500 on upload.
    """
    days = days_for(category, location)
    if days is None:
        return None
    try:
        bought = date.fromisoformat(purchased_on)
    except (TypeError, ValueError):
        return None
    return (bought + timedelta(days=days)).isoformat()

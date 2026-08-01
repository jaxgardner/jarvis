"""The shelf-life table.

Pure functions, no database, no model. This is the file you edit when milk is
consistently wrong — the point of the design is that one edit here fixes every
future gallon.
"""

from pantry import shelflife


def test_a_known_category_gets_its_documented_life():
    assert shelflife.days_for("milk", "fridge") == 7


def test_location_changes_the_answer():
    """The same food frozen lasts far longer. A table keyed only by category
    would have to pick one and be wrong about the other."""
    assert shelflife.days_for("chicken", "fridge") == 2
    assert shelflife.days_for("chicken", "freezer") > 100


def test_an_unknown_category_has_no_date():
    """None means 'nobody knows', which the review screen shows as an empty
    field. A guess wearing a real date's clothes is worse than a blank."""
    assert shelflife.days_for("plutonium", "fridge") is None
    assert shelflife.days_for(None, "fridge") is None


def test_shelf_stable_goods_have_no_date():
    assert shelflife.days_for("salt", "pantry") is None


def test_expires_on_adds_days_to_the_purchase_date():
    assert shelflife.expires_on("milk", "fridge", "2026-07-31") == "2026-08-07"


def test_expires_on_crosses_a_month_boundary():
    assert shelflife.expires_on("spinach", "fridge", "2026-07-30") == "2026-08-04"


def test_expires_on_returns_none_when_the_category_has_no_life():
    assert shelflife.expires_on("salt", "pantry", "2026-07-31") is None
    assert shelflife.expires_on(None, "fridge", "2026-07-31") is None


def test_a_malformed_purchase_date_yields_no_date_rather_than_raising():
    """Extraction can misread the date off a crumpled receipt. That must cost
    a blank field, not a 500 on upload."""
    assert shelflife.expires_on("milk", "fridge", "not-a-date") is None


def test_every_category_has_a_default_location():
    missing = [c for c in shelflife.CATEGORIES if c not in shelflife.DEFAULT_LOCATION]
    assert missing == [], f"categories with no default location: {missing}"


def test_every_default_location_is_a_real_location():
    assert set(shelflife.DEFAULT_LOCATION.values()) <= {"fridge", "freezer", "pantry"}


def test_categories_is_sorted_and_unique():
    """It is interpolated into a tool schema enum, which must be byte-stable
    across runs or the prompt changes for no reason."""
    assert shelflife.CATEGORIES == sorted(set(shelflife.CATEGORIES))

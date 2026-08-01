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


# ── guessing a category from a typed name ─────────────────


def test_a_plain_category_name_matches_itself():
    assert shelflife.guess_category("milk") == "milk"
    assert shelflife.guess_category("spinach") == "spinach"


def test_a_qualified_name_still_finds_its_category():
    """What you actually type: 'whole milk', not 'milk'."""
    assert shelflife.guess_category("whole milk") == "milk"
    assert shelflife.guess_category("chicken breast") == "chicken"
    assert shelflife.guess_category("baby spinach") == "spinach"
    assert shelflife.guess_category("Greek Yogurt") == "yogurt"


def test_matching_is_by_word_not_substring():
    """The trap this exists to avoid: 'tea' is a substring of 'steak', so
    naive matching would file a steak under tea and give it a two-year life."""
    assert shelflife.guess_category("steak") != "tea"
    assert shelflife.guess_category("cereal") == "cereal"


def test_a_multi_word_category_needs_all_its_words():
    assert shelflife.guess_category("hard cheese") == "cheese_hard"
    assert shelflife.guess_category("fresh herbs") == "herbs_fresh"


def test_an_unknown_name_has_no_category():
    """Which means no date, which the review screen shows as a blank for you
    to fill in. A wrong guess is worse than an admitted blank."""
    assert shelflife.guess_category("plutonium") is None
    assert shelflife.guess_category("") is None
    assert shelflife.guess_category(None) is None


def test_a_guessed_category_is_a_real_table_key():
    """Whatever comes out must be usable by days_for, or the date silently
    goes missing."""
    for name in ("whole milk", "chicken breast", "sourdough bread", "olive oil"):
        category = shelflife.guess_category(name)
        assert category in shelflife.CATEGORIES, name

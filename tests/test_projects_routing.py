"""The tool definitions and the prompt block, checked without spending money.

Whether Haiku actually picks them lives in tests/test_project_utterances.py,
which runs against the live model.
"""

from app import router


def tool(name: str) -> dict:
    return next(t for t in router.TOOLS if t["name"] == name)


def test_start_project_takes_a_name_and_an_optional_research_task():
    schema = tool("start_project")["input_schema"]
    assert schema["required"] == ["name"]
    assert set(schema["properties"]) == {"name", "description", "research_task"}


def test_the_writers_take_an_id_and_never_a_name():
    for name in ("add_note", "add_event", "add_reminder", "escalate"):
        properties = tool(name)["input_schema"]["properties"]
        assert properties["project_id"]["type"] == "integer"

    # The free-text name is what let a misheard word spawn a ghost project.
    assert "project" not in tool("add_note")["input_schema"]["properties"]


def test_query_can_be_asked_about_a_project():
    properties = tool("query")["input_schema"]["properties"]
    assert "project" in properties["kind"]["enum"]
    assert properties["project_id"]["type"] == "integer"


# The block's opening line. Asserted on rather than the bare word "PROJECTS",
# which also appears in the tool-choice instructions and always will.
PROJECTS_HEADER = "PROJECTS — the user's active projects"


def test_the_projects_block_is_omitted_entirely_when_there_are_none():
    """An empty table invites the model to invent an id — the same reason the
    REPORTS block is omitted rather than rendered empty."""
    prompt = router.system_prompt("America/Denver", projects=())
    assert PROJECTS_HEADER not in prompt


def test_the_projects_block_lists_ids_and_names():
    prompt = router.system_prompt(
        "America/Denver",
        projects=[{"id": 3, "name": "hydroponic lettuce"}],
    )
    assert PROJECTS_HEADER in prompt
    assert "  3     hydroponic lettuce" in prompt


def test_the_system_prompt_says_when_to_start_one():
    prompt = router.system_prompt("America/Denver")
    assert "start_project" in prompt


def test_every_tool_still_has_somewhere_to_go():
    from app import handlers
    from app.main import DEEP_TOOLS

    for entry in router.TOOLS:
        assert entry["name"] in handlers.FAST_HANDLERS or entry["name"] in DEEP_TOOLS

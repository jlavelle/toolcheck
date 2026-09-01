"""pi vocabulary normalisation: mapping table cases and laws."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from toolcheck.pi import normalise


def test_tool_name_table():
    assert normalise("bash", {"command": "ls"})[0] == "Bash"
    assert normalise("read", {})[0] == "Read"
    assert normalise("write", {})[0] == "Write"
    assert normalise("edit", {})[0] == "Edit"
    assert normalise("grep", {})[0] == "Grep"


def test_path_maps_to_file_path():
    _, inp = normalise("read", {"path": "/x.py", "offset": 3, "limit": 10})
    assert inp == {"file_path": "/x.py", "offset": 3, "limit": 10}


def test_write_and_edit_paths_map():
    assert normalise("write", {"path": "/x"}) == ("Write", {"file_path": "/x"})
    assert normalise("edit", {"path": "/x", "edits": [{"oldText": "a"}]}) == (
        "Edit",
        {"file_path": "/x", "edits": [{"oldText": "a"}]},
    )


def test_bash_and_grep_names_map_inputs_pass_through():
    assert normalise("bash", {"command": "ls", "timeout": 5}) == (
        "Bash",
        {"command": "ls", "timeout": 5},
    )
    assert normalise("grep", {"pattern": "x", "glob": "*.py"}) == (
        "Grep",
        {"pattern": "x", "glob": "*.py"},
    )


def test_unknown_tool_passes_through_identically():
    assert normalise("find", {"pattern": "*.py"}) == ("find", {"pattern": "*.py"})
    assert normalise("ls", {"path": "/tmp"}) == ("ls", {"path": "/tmp"})


def test_canonical_name_is_not_double_mapped():
    # "Read" is not a pi tool name; a pi-specific payload naming it stays put.
    assert normalise("Read", {"path": "/x"}) == ("Read", {"path": "/x"})


@given(
    tool=st.sampled_from(["bash", "read", "write", "edit", "grep"]),
    tool_input=st.dictionaries(
        # pi tool schemas never name a field file_path
        st.text().filter(lambda k: k != "file_path"),
        st.integers(),
        max_size=6,
    ),
)
def test_mapping_is_rename_or_identity(tool: str, tool_input: dict[str, int]):
    """Each input key is renamed (path → file_path) or preserved, never dropped."""
    _, mapped = normalise(tool, tool_input)
    path_tool = tool in ("read", "write", "edit")
    for field, value in tool_input.items():
        if path_tool and field == "path":
            assert mapped.get("file_path") == value
            assert "path" not in mapped
        else:
            assert mapped[field] == value
    expected = {
        "file_path" if path_tool and k == "path" else k for k in tool_input
    }
    assert set(mapped) == expected


@given(
    tool=st.sampled_from(["read", "write", "edit"]),
    tool_input=st.dictionaries(
        st.text().filter(lambda k: k != "file_path"), st.integers(), max_size=6
    ),
)
def test_file_path_present_iff_path_was(tool: str, tool_input: dict[str, int]):
    _, mapped = normalise(tool, tool_input)
    assert ("file_path" in mapped) == ("path" in tool_input)
    if "path" in tool_input:
        assert mapped["file_path"] == tool_input["path"]


@given(
    tool=st.text().filter(lambda t: t not in ("bash", "read", "write", "edit", "grep")),
    tool_input=st.dictionaries(st.text(), st.integers(), max_size=6),
)
def test_unknown_tool_passes_through_identically_property(
    tool: str, tool_input: dict[str, int]
):
    assert normalise(tool, tool_input) == (tool, tool_input)

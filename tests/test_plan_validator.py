"""Plan-validator helper tests (post loop-removal).

The LLM plan validator (``PlanValidator``/``PlanRejectedError``) was REMOVED with the
run_goal loop family. What survives in :mod:`computer_use_mcp.plan_validator` are the
two PURE helpers the live checkpoint/resume path consumes — ``find_cycle`` (checkpoint
payload validation, ``SubtaskManager.restore``) and ``contains_control_characters``
(checkpoint payload validation). These tests pin exactly those helpers.
Pure domain tests — no network, no provider, no execution.
"""

from __future__ import annotations

from computer_use_mcp.plan_validator import contains_control_characters, find_cycle

# --- shared cycle finder ------------------------------------------------------------------

def test_find_cycle_deterministic_and_acyclic_safe() -> None:
    assert find_cycle({}) is None
    assert find_cycle({"a": [], "b": ["a"], "c": ["b"]}) is None
    cycle = find_cycle({"a": ["c"], "b": ["a"], "c": ["b"]})
    assert cycle == ("a", "c", "b", "a")  # sorted start node, sorted neighbors
    assert find_cycle({"a": ["b"], "b": ["a"]}) == ("a", "b", "a")
    # Dependencies outside the node set are ignored here (unknown-dep rule is separate).
    assert find_cycle({"a": ["ghost"]}) is None


def test_find_cycle_empty_dependency_list_is_acyclic() -> None:
    assert find_cycle({"a": [], "b": [], "c": []}) is None


def test_find_cycle_self_dependency_is_a_cycle() -> None:
    assert find_cycle({"a": ["a"]}) == ("a", "a")


def test_find_cycle_reports_same_cycle_regardless_of_input_order() -> None:
    forward = {"a": ["b"], "b": ["c"], "c": ["a"]}
    reverse = {"c": ["a"], "b": ["c"], "a": ["b"]}
    assert find_cycle(forward) == find_cycle(reverse) == ("a", "b", "c", "a")


def test_find_cycle_long_chain_with_back_edge() -> None:
    graph = {"n1": ["n2"], "n2": ["n3"], "n3": ["n4"], "n4": ["n2"]}
    assert find_cycle(graph) == ("n2", "n3", "n4", "n2")


def test_find_cycle_ignores_dangling_dependencies() -> None:
    # Every dependency referencing an unknown id is skipped (documented contract).
    assert find_cycle({"a": ["ghost", "phantom"]}) is None


# --- control-character detector -----------------------------------------------------------

def test_contains_control_characters_rejects_c0_and_del() -> None:
    assert contains_control_characters("bad \x00 secret") is True
    assert contains_control_characters("bad \x1b escape") is True
    assert contains_control_characters("bad \x7f del") is True
    assert contains_control_characters("newline \n inside") is True
    assert contains_control_characters("tab \t inside") is True


def test_contains_control_characters_accepts_clean_text() -> None:
    assert contains_control_characters("") is False
    assert contains_control_characters("open the workbook and filter the data") is False
    # Non-ASCII printable text (em dash, CJK) is NOT a control character.
    assert contains_control_characters("öffnet die Map — 地图") is False

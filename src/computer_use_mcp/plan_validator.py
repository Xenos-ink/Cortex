"""Pure graph/content helpers shared by the checkpoint and subtask domain.

Post loop-removal: the LLM plan validator (``PlanValidator``/``PlanRejectedError``/
``PlanValidationResult``) was removed with the run_goal loop family that consumed it.
What survives are the two PURE helpers the live checkpoint/resume path uses:

- :func:`find_cycle` — deterministic dependency-cycle detector (checkpoint_manager and
  SubtaskManager.restore re-check restored graphs);
- :func:`contains_control_characters` — C0/DEL detector for ids and descriptions
  (checkpoint_manager).

Both are deterministic, side-effect free, and bounded; see their docstrings.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = [
    "contains_control_characters",
    "find_cycle",
]


def find_cycle(depends_on_by_id: Mapping[str, Iterable[str]]) -> tuple[str, ...] | None:
    """Return one dependency cycle as ``(a, ..., a)`` or ``None`` when the graph is acyclic.

    Deterministic: start nodes and neighbor sets are visited in sorted order, so the same
    graph always yields the same cycle. Dependencies referencing ids outside the node set
    are ignored here (callers enforce the unknown-dependency rule separately).
    """
    color: dict[str, int] = {node: 0 for node in depends_on_by_id}  # 0 white, 1 gray, 2 black
    for start in sorted(color):
        if color[start] != 0:
            continue
        color[start] = 1
        path = [start]
        stack = [iter(sorted(depends_on_by_id[start]))]
        while stack:
            advanced = False
            for dep in stack[-1]:
                if dep not in color:
                    continue
                if color[dep] == 1:
                    return tuple(path[path.index(dep):]) + (dep,)
                if color[dep] == 0:
                    color[dep] = 1
                    path.append(dep)
                    stack.append(iter(sorted(depends_on_by_id[dep])))
                    advanced = True
                    break
            if not advanced:
                color[path.pop()] = 2
                stack.pop()
    return None


def contains_control_characters(text: str) -> bool:
    """True when ``text`` contains C0 control characters or DEL (never safely surfaceable)."""
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)

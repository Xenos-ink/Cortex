"""RecordingEngine: InputEngine stub shared by backend unit tests (no real input).

Lives in its own uniquely-named top-level module (NOT conftest.py) because the test
tree is imported root-less (no ``__init__.py``): ``conftest`` is ambiguous between
``tests/`` and ``tests/e2e/`` during full-suite collection, while ``recording_engine``
resolves uniquely to this file.
"""

from __future__ import annotations

from computer_use_mcp.backend import InputEngine


class RecordingEngine(InputEngine):
    """InputEngine stand-in recording every engine call (no real input dispatch).

    The unit suite drives ``LocalComputerBackend.execute`` through this stub instead of
    the real SendInput/pyautogui engines, so stop-check/release/argument contracts are
    verified without touching the shared live desktop.
    """

    def __init__(self, *, drag_interpolate: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.down = False
        self.click_interval = 0.0
        self.type_interval = 0.0
        self.drag_interpolate = drag_interpolate
        self.drag_step_pause = 0.0

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", x, y))

    def click(self, x: int, y: int, clicks: int = 1) -> None:
        self.calls.append(("click", x, y, clicks))

    def mouse_down(self, button: str = "left") -> None:
        self.down = True
        self.calls.append(("mouse_down", button))

    def mouse_up(self, button: str = "left") -> None:
        self.down = False
        self.calls.append(("mouse_up", button))

    def type_text(self, text: str, before_chunk: object = None) -> None:
        if before_chunk is not None:
            before_chunk()  # mirror the real engines' pre-chunk stop check
        self.calls.append(("type_text", text))

    def chord(self, keys: list[str]) -> None:
        self.calls.append(("chord", *keys))

    def scroll(self, delta: int) -> None:
        self.calls.append(("scroll", delta))

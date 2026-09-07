"""A faithful fake desktop for benchmark ``fake`` mode — the whole harness, no GUI.

Models exactly the surface the runtime touches, using the runtime's own contracts:

- top-level windows with real ``WindowInfo`` identity and bounds; click hit-testing
  switches focus like a real window manager (topmost window under the point wins);
- a Notepad-like edit buffer per window, exposed to the runtime through
  ``Observation.ocr_text`` (the designed P1 perception seam) so
  ``TextPredicateStrategy``/``expected_text`` verification runs the real code path;
- a classic-Calculator model (display, operator, equals) whose buttons are hit-tested
  against a modeled grid laid out like the real ``CalcFrame`` geometry;
- ``ctrl+s`` on a Notepad window models "save" by writing the edit buffer to the file —
  so file-based verification predicates run in fake mode too;
- faults: ``move_window`` / ``focus`` mutate the world between propose and execute.

This is harness validation, not benchmark evidence about real applications.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from computer_use_mcp.backend import FakeComputerBackend
from computer_use_mcp.models import GroundedAction, TextRegion, WindowInfo

from .appwin import CALC_BUTTON_LAYOUT


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


class FakeCalculator:
    """Classic-calculator state machine (digits, . + - * / = C)."""

    def __init__(self) -> None:
        self.display = "0"
        self._accumulator: float | None = None
        self._operator: str | None = None
        self._fresh = True

    def press(self, label: str) -> None:
        if label.isdigit():
            if self._fresh:
                self.display = label
                self._fresh = False
            else:
                self.display = label if self.display == "0" else self.display + label
        elif label == ".":
            if self._fresh:
                self.display, self._fresh = "0.", False
            elif "." not in self.display:
                self.display += "."
        elif label in {"+", "-", "*", "/"}:
            self._accumulator = float(self.display) if self._accumulator is None else self._accumulator
            self._operator = label
            self._fresh = True
        elif label == "=":
            if self._operator is not None and self._accumulator is not None:
                operand = float(self.display)
                operations = {
                    "+": lambda a, b: a + b,
                    "-": lambda a, b: a - b,
                    "*": lambda a, b: a * b,
                    "/": lambda a, b: a / b,
                }
                result = operations[self._operator](self._accumulator, operand)
                self.display = _format_number(result)
                self._accumulator = None
                self._operator = None
                self._fresh = True
        elif label == "C":
            self.display = "0"
            self._accumulator = None
            self._operator = None
            self._fresh = True


class FakeWorldBackend(FakeComputerBackend):
    """FakeComputerBackend + a minimal window manager / app model (see module docstring)."""

    BUTTON_SIZE = (43, 33)
    BUTTON_ORIGIN_OFFSET = (22, 145)
    COLUMN_PITCH = 49
    ROW_PITCH = 40
    HIT_TOLERANCE = 26

    def __init__(self, *, width: int = 1920, height: int = 1080, **kwargs: Any) -> None:
        super().__init__(width=width, height=height, **kwargs)
        self.windows: dict[str, WindowInfo] = {}
        self.window_kinds: dict[str, str] = {}
        self.window_texts: dict[str, str] = {}
        self.window_files: dict[str, Path] = {}
        self.focused: str | None = None
        self.calculator = FakeCalculator()
        self.z_order: list[str] = []  # back-to-front
        self.executed_labels: list[str] = []
        self._executes = 0

    # -- world management ------------------------------------------------------------------

    def add_window(
        self,
        key: str,
        *,
        title: str,
        process_name: str,
        window_class: str,
        bounds: tuple[int, int, int, int],
        kind: str = "notepad",
        file_path: Path | None = None,
        text: str = "",
    ) -> None:
        info = WindowInfo(
            hwnd=47000 + len(self.windows),
            pid=31000 + len(self.windows),
            process_name=process_name,
            exe_path=f"C:\\Windows\\System32\\{process_name}",
            window_class=window_class,
            title=title,
            bounds=bounds,
        )
        self.windows[key] = info
        self.window_kinds[key] = kind
        self.window_texts[key] = text
        if file_path is not None:
            self.window_files[key] = file_path
        if kind == "calculator":
            self.calculator = FakeCalculator()
        self.z_order.append(key)
        self.focus(key)

    def focus(self, key: str) -> None:
        if key not in self.windows:
            raise KeyError(f"unknown fake window {key!r}")
        self.focused = key
        self.active_window = self.windows[key]

    def move_window(self, key: str, bounds: tuple[int, int, int, int]) -> None:
        if key not in self.windows:
            raise KeyError(f"unknown fake window {key!r}")
        self.windows[key] = self.windows[key].model_copy(update={"bounds": bounds})
        if self.focused == key:
            self.active_window = self.windows[key]

    def window_bounds(self, key: str) -> tuple[int, int, int, int]:
        return self.windows[key].bounds  # type: ignore[return-value]

    def window_text(self, key: str) -> str:
        return self.window_texts.get(key, "")

    def button_grid(self, key: str) -> dict[str, tuple[int, int]]:
        """Modeled button grid (absolute screenshot coordinates) for a calculator window."""
        return self._fake_grid(key)

    def _fake_grid(self, key: str) -> dict[str, tuple[int, int]]:
        left, top, _, _ = self.window_bounds(key)
        origin_x = left + self.BUTTON_ORIGIN_OFFSET[0]
        origin_y = top + self.BUTTON_ORIGIN_OFFSET[1]
        grid: dict[str, tuple[int, int]] = {}
        for label, (col, row, col_span, row_span) in CALC_BUTTON_LAYOUT.items():
            grid[label] = (
                origin_x + col * self.COLUMN_PITCH + (self.BUTTON_SIZE[0] * col_span) // 2,
                origin_y + row * self.ROW_PITCH + (self.BUTTON_SIZE[1] * row_span) // 2,
            )
        return grid

    # -- runtime contract ------------------------------------------------------------------

    def observe(self, monitor_index: int | None = None):  # type: ignore[no-untyped-def]
        observation = super().observe(monitor_index)
        # Alternate screenshot colors per completed execute so the runtime's pixel-diff
        # verification sees the change a real screen would show (faithful modeling).
        if self._executes % 2 == 1:
            observation.image_base64 = self._dark_png()
        text = self.window_texts.get(self.focused or "", "")
        observation.ocr_text = [
            TextRegion(text=text, x=8, y=8, width=200, height=16, confidence=0.99)
        ] if text else []
        return observation

    def _dark_png(self) -> str:
        import base64
        import io

        from PIL import Image

        image = Image.new("RGB", (self.width, self.height), (24, 24, 24))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def execute(
        self,
        action: GroundedAction,
        stop: Any = None,
        focus_hook: Any = None,
        allow_launch: bool = False,
    ) -> str:
        message = super().execute(action, stop, focus_hook=focus_hook, allow_launch=allow_launch)
        self._executes += 1
        self._model_effect(action)
        return message

    def _model_effect(self, action: GroundedAction) -> None:
        kind = action.action.value if hasattr(action.action, "value") else str(action.action)
        if kind == "type" and action.text and self.focused is not None:
            if self.window_kinds.get(self.focused) == "calculator":
                for char in action.text:
                    if char.isdigit() or char in {".", "+", "-", "*", "=", "C"}:
                        self.calculator.press(char)
                        self.executed_labels.append(char)
            else:
                self.window_texts[self.focused] = self.window_texts.get(self.focused, "") + action.text
        elif kind == "keypress" and action.keys and self.focused in self.window_files:
            keys = [key.casefold() for key in action.keys]
            if keys == ["ctrl", "s"]:
                self.window_files[self.focused].write_text(
                    self.window_texts.get(self.focused, ""), encoding="utf-8"
                )
        elif kind == "click" and action.point is not None:
            self._model_click(int(action.point.x), int(action.point.y))

    def _model_click(self, x: int, y: int) -> None:
        # Focus follows the mouse: topmost window containing the point wins.
        topmost = self.focused
        for key in reversed(self.z_order):
            left, top, width, height = self.window_bounds(key)
            if left <= x < left + width and top <= y < top + height:
                topmost = key
                break
        if topmost != self.focused and topmost is not None:
            self.focus(topmost)
        if self.focused is None or self.window_kinds.get(self.focused) != "calculator":
            return
        grid = self._fake_grid(self.focused)
        nearest = min(
            grid.items(),
            key=lambda item: (item[1][0] - x) ** 2 + (item[1][1] - y) ** 2,
            default=None,
        )
        if nearest is not None:
            label, (bx, by) = nearest
            if (bx - x) ** 2 + (by - y) ** 2 <= self.HIT_TOLERANCE**2:
                self.calculator.press(label)
                self.executed_labels.append(label)

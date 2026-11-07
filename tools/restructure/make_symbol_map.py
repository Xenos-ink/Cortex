"""Build the WP-R2a symbol_map.json from before_symbols.json (step-1 moves only).

Usage: python tools/restructure/make_symbol_map.py <before_symbols.json> <out.json>

Every `computer_use_mcp.backend` symbol is routed to its new desktop/ module by the
NAME table below (plan §2); any backend symbol missing from the table, and any
statement not listed in STATEMENT_ROUTES, is a hard error (fail closed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TABLE: dict[str, str] = {}

for _name in (
    "IS_WINDOWS GA_ROOT PROCESS_QUERY_LIMITED_INFORMATION MDT_EFFECTIVE_DPI MONITORINFOF_PRIMARY"
    " DEFAULT_DPI DPI_AWARENESS_CONTEXT_PER_MONITOR_V2 MONITORINFOEXW _MONITORENUMPROC"
    " _user32 _kernel32 _shcore _ole32 _oleaut32 _MOUSEINPUT _KEYBDINPUT _INPUT_UNION _INPUT"
    " _INPUT_MOUSE _INPUT_KEYBOARD _VARIANT _GUID _GUITHREADINFO _GW_CHILD _GW_HWNDNEXT"
    " _WM_GETTEXT _WM_GETTEXTLENGTH _SMTO_ABORTIFHUNG _SENDMESSAGE_TIMEOUT_MS _MOUSEEVENTF_MOVE"
    " _MOUSEEVENTF_LEFTDOWN _MOUSEEVENTF_LEFTUP _MOUSEEVENTF_RIGHTDOWN _MOUSEEVENTF_RIGHTUP"
    " _MOUSEEVENTF_MIDDLEDOWN _MOUSEEVENTF_MIDDLEUP _MOUSEEVENTF_WHEEL _MOUSEEVENTF_VIRTUALDESK"
    " _MOUSEEVENTF_ABSOLUTE _KEYEVENTF_UNICODE _SM_XVIRTUALSCREEN _SM_YVIRTUALSCREEN"
    " _SM_CXVIRTUALSCREEN _SM_CYVIRTUALSCREEN _COINIT_APARTMENTTHREADED _RPC_E_CHANGED_MODE"
    " _CLSCTX_INPROC_SERVER _TREE_SCOPE_CHILDREN _VT_EMPTY _VT_I4 _VT_BSTR _VT_BOOL _VT_ARRAY"
    " _VT_R8 _SW_RESTORE _VK_MENU _KEYEVENTF_KEYUP"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.win32"

for _name in (
    "_GW_OWNER _DIALOG_WINDOW_CLASS _set_process_dpi_awareness _query_current_dpi_awareness"
    " _window_text _window_class_name _window_pid _process_image_path _window_rect"
    " query_foreground_window _title_match_rank find_window_by_title get_cursor_position"
    " query_focus_target is_window_owned_by _owner_chain_roots detect_system_dialog"
    " enumerate_app_windows _MODIFIER_KEY_NAMES query_stuck_modifiers _system_dpi_scale"
    " _monitor_is_primary _monitors_via_enum_display_monitors _monitors_via_mss_list"
    " enumerate_monitors _select_target_monitor"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.windows"

for _name in (
    "BackendError InputBlockedError DisplayUnavailableError CoordinateSpaceError"
    " UnsupportedActionError WindowFocusError FocusDriftError TextIntegrityError"
    " LaunchTargetError"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.errors"

for _name in (
    "LAUNCH_NEEDLE_PATTERN validate_launch_needle LAUNCH_PATHS_ENV parse_launch_paths"
    " _mapped_launch_path _launch_unresolved_suffix UNSAVED_DOC_LEADING_TOKENS"
    " _UNSAVED_RESTORE_SUFFIX AppWindowCandidate doc_token_from_title"
    " is_unsaved_candidate_title"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.launch"

for _name in (
    "_SCALE_TOLERANCE CoordinateTransform CoordinateVerdict classify_coordinate_space"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.coordinates"

for _name in (
    "WAIT_MAX_SECONDS WAIT_SLICE_SECONDS WAIT_CLAMPED_MARKER _DRAG_SEGMENT_PIXELS"
    " _DRAG_MIN_SEGMENTS _DRAG_STEP_PAUSE_SECONDS interruptible_wait _sleep_for_wait_action"
    " _wait_clamp_note HOLD_SLICE_SECONDS _sleep_for_hold _wait_for_hold _drag_segment_points"
    " _MOTION_SHAPES _MOTION_EASINGS _MOTION_MAX_POINTS _MOTION_MIN_SEGMENTS _motion_path_points"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.motion"

for _name in (
    "_TYPE_INTERVAL_SECONDS INPUT_BACKEND_ENV PYAUTOGUI_PAUSE_ENV TYPE_INTERVAL_ENV"
    " DRAG_INTERPOLATE_ENV DRAG_STEP_PAUSE_ENV DRAG_STEP_PIXELS_ENV SENDINPUT_TYPE_INTERVAL"
    " SENDINPUT_DRAG_STEP_PAUSE SENDINPUT_DRAG_STEP_PIXELS _SENDINPUT_FACTORY_DRAG_STEP_PAUSE"
    " _SENDINPUT_FACTORY_DRAG_STEP_PIXELS _SENDINPUT_CHUNK_EVENTS _WHEEL_DELTA _VK_NAMED_KEYS"
    " _MOUSE_BUTTON_FLAGS _virtual_screen_metrics _primary_screen_size _to_absolute_65535"
    " _from_absolute_65535 _mouse_move_event _mouse_flag_event _key_event _text_to_key_units"
    " _resolve_key_events _chord_key_units _chord_key_halves InputEngine PyAutoGuiInputEngine"
    " SendInputEngine"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.input_engine"

for _name in (
    "_CF_UNICODETEXT _GMEM_MOVEABLE _CLIPBOARD_OPEN_ATTEMPTS _CLIPBOARD_OPEN_RETRY_SECONDS"
    " _CLIPBOARD_ARGTYPES_BOUND _clipboard_bind _clipboard_open CLIPBOARD_RESTORE_FAILED"
    " CLIPBOARD_RESTORE_DESTROYED CLIPBOARD_RESTORABLE CLIPBOARD_NON_TEXT CLIPBOARD_UNKNOWN"
    " _clipboard_read_verdict _clipboard_read_text _clipboard_write_text"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.clipboard"

for _name in (
    "UIA_READ_ENV _WINDOW_TEXT_MAX_CHARS UIA_MAX_ELEMENTS UIA_MAX_DEPTH"
    " UIA_READ_BUDGET_SECONDS SNAPSHOT_DROPPED_KEY SNAPSHOT_DROP_REASONS _empty_drops"
    " snapshot_dropped_total _guid_from_string _com_call _com_release UiaSemanticReader"
    " _WIN32_CLASS_CONTROL_TYPES Win32TextReader _window_child_chain _window_text_via_message"
    " _safe_array_doubles _uia_rect_to_screenshot _uia_snapshot_to_ui_elements"
    " UIA_MAX_LABEL_CHARS _uia_snapshot_to_text_regions _UIA_PROP_BOUNDING_RECTANGLE"
    " _UIA_PROP_CONTROL_TYPE _UIA_PROP_NAME _UIA_PROP_AUTOMATION_ID _UIA_PROP_VALUE"
    " _UIA_PROP_IS_OFFSCREEN _CLSID_CUIAUTOMATION _IID_IUIAUTOMATION _UIA_GET_FOCUSED_ELEMENT"
    " _UIA_CREATE_TRUE_CONDITION _UIA_ELEMENT_FIND_ALL _UIA_ELEMENT_GET_PROPERTY_VALUE"
    " _UIA_ARRAY_GET_LENGTH _UIA_ARRAY_GET_ELEMENT _COM_RELEASE UIA_CONTROLTYPE_NAMES"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.text_readers"

for _name in (
    "PNG_OPTIMIZE_ENV PNG_COMPRESS_LEVEL_ENV INTERNAL_FRAME_REUSE_ENV CAPTURE_BACKEND_ENV"
    " _DXGI_ACQUIRE_TIMEOUT_MS TEXT_PNG_ENV _DXGI_PAYLOAD_CACHE _CaptureContext"
    " _DxgiDuplicator _raw_payload_key _payload_is_raw_key"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.capture"

for _name in (
    "TERMINAL_KEYS _env_bool _env_nonnegative_float _env_finite_float _env_bounded_float"
    " _env_bounded_int _env_nonnegative_int _env_int _env_choice _env_positive_int"
    " _env_int_in_range KEY_DISPATCH_GAP_SECONDS FOCUS_TRANSITION_SETTLE_SECONDS"
    " TYPE_INTEGRITY_ENABLED TYPE_CHUNK_CHARS TYPE_VERIFY_SETTLE_SECONDS"
    " TYPE_VERIFY_CONFIRM_GAP_SECONDS TYPE_VERIFY_FAST_MAX_READS TYPE_VERIFY_STABILITY_SECONDS"
    " TYPE_MAX_RETYPES_PER_CHUNK MOVE_DURATION_ENV PLAN_MOVE_DURATION_ENV MOVE_SHAPE_ENV"
    " MOVE_BEND_PX_ENV MOVE_EASING_ENV PLAN_SAMPLE_INTERVAL_ENV MOVE_SETTLE_ENV"
    " MOVE_DURATION_MS PLAN_MOVE_DURATION_MS MOVE_SHAPE MOVE_BEND_PX MOVE_EASING"
    " PLAN_SAMPLE_INTERVAL_MS MOVE_SETTLE_MS _MOVE_SETTLE_SECONDS _ALWAYS_VALUED_CONTROL_TYPES"
    " _landed_prefix_len _normalize_landed_text _split_type_chunks _typed_index_for_norm_len"
    " _format_integrity_status TYPE_UNCONFIRMED_MARKER TYPE_UNCONFIRMED_HINT_SENDINPUT"
    " TYPE_UNCONFIRMED_HINT_CLIPBOARD _validate_action_defense _plan_point_settle_seconds"
    " _mark_action_dispatched ComputerBackend LocalComputerBackend"
).split():
    TABLE[_name] = "computer_use_mcp.desktop.backend"

TABLE["FakeComputerBackend"] = "computer_use_mcp.desktop.fake_backend"

#: backend's 4 non-simple top-level statements (step-0 notes §4/F15): the
#: `if IS_WINDOWS:` ctypes block and the three `_VK_NAMED_KEYS.update` Exprs.
STATEMENT_ROUTES: dict[str, str] = {
    "computer_use_mcp.backend:stmt@fc4bb3c1": "computer_use_mcp.desktop.win32",
    "computer_use_mcp.backend:stmt@5051b7b3": "computer_use_mcp.desktop.input_engine",
    "computer_use_mcp.backend:stmt@f9326db0": "computer_use_mcp.desktop.input_engine",
    "computer_use_mcp.backend:stmt@3c631084": "computer_use_mcp.desktop.input_engine",
}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: make_symbol_map.py <before_symbols.json> <out.json>", file=sys.stderr)
        return 2
    before = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    symbol_map: dict[str, str] = {}
    missing: list[str] = []
    for entry in before["symbols"]:
        if entry["module"] != "computer_use_mcp.backend":
            continue
        name = entry["name"]
        target = TABLE.get(name)
        if target is None:
            missing.append(name)
            continue
        symbol_map[f"computer_use_mcp.backend:{name}"] = target
    for entry in before.get("statements", []):
        if entry["module"] != "computer_use_mcp.backend":
            continue
        key = f"computer_use_mcp.backend:stmt@{entry['hash'][:8]}"
        target = STATEMENT_ROUTES.get(key)
        if target is None:
            missing.append(key)
            continue
        symbol_map[key] = target
    if missing:
        print("unmapped backend entries:", missing, file=sys.stderr)
        return 1
    out = {"symbol_map": symbol_map}
    Path(argv[2]).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(symbol_map)} routes ({len(STATEMENT_ROUTES)} statements)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

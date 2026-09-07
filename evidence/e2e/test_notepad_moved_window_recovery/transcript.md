# E2E transcript — test_notepad_moved_window_recovery

- run_id: `20260907-185134`
- outcome: **passed**
- elapsed: 2179 ms

## Assertions
- [PASS] window actually moved between propose and execute — {'hook': 'move_window', 'before': (60, 120, 700, 450), 'after': (1200, 600, 420, 380)}
- [PASS] stale click's verification FAILED (semantic, not pixel)
- [PASS] bounded recovery classified and re-decided — ['wrong_window']
- [PASS] task completed; marker only in the target window

## Notes
- Window-bounds-only moves do NOT trip STALE_OBSERVATION (identity checks are hwnd/pid/process/monitor/dimensions/coordinate-space); the semantic verification layer is what catches the miss — documented runtime behavior.

## Recorded events
- `{"event": "observe_before", "at": "2026-09-07T18:51:46.756627+00:00", "observation_id": "997c935dcd814ae695b87fe477cd446f", "active_app": "notepad.exe", "window": {"hwnd": 929170656, "pid": 10356, "process_name": "notepad.exe", "exe_path": "C:\\Windows\\System32\\notepad.exe", "window_class": "Notepad", "title": "moved.txt - Notepad", "bounds": [60, 120, 700, 450]}, "coordinate_space": "verified_passthrough"}`
- `{"event": "run_goal", "at": "2026-09-07T18:51:47.797630+00:00", "ok": true, "termination": "completed", "steps": 3}`
- `{"event": "observe_after", "at": "2026-09-07T18:51:47.917728+00:00", "observation_id": "58e7a83eed8f43e2937867e33a7f4547", "active_app": "notepad.exe", "window": {"hwnd": 929170656, "pid": 10356, "process_name": "notepad.exe", "exe_path": "C:\\Windows\\System32\\notepad.exe", "window_class": "Notepad", "title": "*moved.txt - Notepad", "bounds": [1200, 600, 420, 380]}, "coordinate_space": "verified_passthrough"}`

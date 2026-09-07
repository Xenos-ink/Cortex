# E2E transcript — test_notepad_type_semantic_verification

- run_id: `20260907-185134`
- outcome: **passed**
- elapsed: 1084 ms

## Assertions
- [PASS] run_goal completed; approval budget consumed (1)
- [PASS] semantic verification method == window_text (real Edit text) — Expected text found in the real window text.
- [PASS] independent Edit-control text contains the marker

## Notes
- (none)

## Recorded events
- `{"event": "observe_before", "at": "2026-09-07T18:51:45.367626+00:00", "observation_id": "84c4871cd153434b93408c394ce0fff4", "active_app": "notepad.exe", "window": {"hwnd": 192874182, "pid": 23824, "process_name": "notepad.exe", "exe_path": "C:\\Windows\\System32\\notepad.exe", "window_class": "Notepad", "title": "typed.txt - Notepad", "bounds": [33, 33, 1440, 753]}, "coordinate_space": "verified_passthrough"}`
- `{"event": "run_goal", "at": "2026-09-07T18:51:45.801621+00:00", "ok": true, "termination": "completed", "budget_remaining": 0}`
- `{"event": "observe_after", "at": "2026-09-07T18:51:45.916619+00:00", "observation_id": "ff7b9253e27b4a2787fa9fff79440447", "active_app": "notepad.exe", "window": {"hwnd": 192874182, "pid": 23824, "process_name": "notepad.exe", "exe_path": "C:\\Windows\\System32\\notepad.exe", "window_class": "Notepad", "title": "*typed.txt - Notepad", "bounds": [33, 33, 1440, 753]}, "coordinate_space": "verified_passthrough"}`

# E2E transcript — test_notepad_window_switch_stale_observation

- run_id: `20260907-185134`
- outcome: **passed**
- elapsed: 3154 ms

## Assertions
- [PASS] decoy window focused between propose and validate
- [PASS] click rejected as STALE_OBSERVATION before execution — 2 rejection(s)
- [PASS] runtime re-observed and re-decided (bounded recovery)
- [PASS] marker landed only in the target window

## Notes
- (none)

## Recorded events
- `{"event": "observe_before", "at": "2026-09-07T18:51:49.282632+00:00", "observation_id": "8d22206424af4017a7f3261d8e6a501e", "active_app": "notepad.exe", "window": {"hwnd": 90769574, "pid": 21472, "process_name": "notepad.exe", "exe_path": "C:\\Windows\\System32\\notepad.exe", "window_class": "Notepad", "title": "target_a.txt - Notepad", "bounds": [60, 120, 700, 450]}, "coordinate_space": "verified_passthrough"}`
- `{"event": "run_goal", "at": "2026-09-07T18:51:50.978089+00:00", "ok": true, "termination": "completed", "steps": 2}`
- `{"event": "observe_after", "at": "2026-09-07T18:51:51.098090+00:00", "observation_id": "51a2855fc5e4436c8c4b2b140d6b3a2c", "active_app": "notepad.exe", "window": {"hwnd": 90769574, "pid": 21472, "process_name": "notepad.exe", "exe_path": "C:\\Windows\\System32\\notepad.exe", "window_class": "Notepad", "title": "*target_a.txt - Notepad", "bounds": [210, 220, 700, 450]}, "coordinate_space": "verified_passthrough"}`

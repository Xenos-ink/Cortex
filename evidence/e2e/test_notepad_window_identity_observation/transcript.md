# E2E transcript — test_notepad_window_identity_observation

- run_id: `20260907-185134`
- outcome: **passed**
- elapsed: 573 ms

## Assertions
- [PASS] process_name == notepad.exe
- [PASS] exe_path populated — C:\Windows\System32\notepad.exe
- [PASS] hwnd/pid match the launched process — 10572
- [PASS] window_class/title/bounds populated — identity_probe.txt - Notepad
- [PASS] coordinate_space verified_passthrough @ 125% DPI

## Notes
- (none)

## Recorded events
- `{"event": "observe_before", "at": "2026-09-07T18:51:44.760702+00:00", "observation_id": "2ff68c212af84d67b6688753a5e163d0", "active_app": "notepad.exe", "window": {"hwnd": 83430004, "pid": 10572, "process_name": "notepad.exe", "exe_path": "C:\\Windows\\System32\\notepad.exe", "window_class": "Notepad", "title": "identity_probe.txt - Notepad", "bounds": [33, 33, 1440, 753]}, "coordinate_space": "verified_passthrough"}`

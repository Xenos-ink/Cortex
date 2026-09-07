# E2E run history (one line per test per run, appended by the evidence fixture)

Note: summary lines from intermediate debugging runs (run ids 20260905-2039xx..2050xx,
which predated the final suite code) were pruned before committing; the committed
evidence directories contain only the final green run artifacts.

- 20260905-205303 | test_browser_local_page_window_state_verification | passed | 5472 ms
- 20260905-205303 | test_calculator_clicks_and_display_verification | passed | 13335 ms
- 20260905-205303 | test_calculator_division_precision | passed | 9769 ms
- 20260905-205303 | test_notepad_window_identity_observation | passed | 754 ms
- 20260905-205303 | test_notepad_type_semantic_verification | passed | 6203 ms
- 20260905-205303 | test_notepad_moved_window_recovery | passed | 10935 ms
- 20260905-205303 | test_notepad_window_switch_stale_observation | passed | 10451 ms
- 20260905-210103 | test_browser_local_page_window_state_verification | passed | 5986 ms
- 20260905-210103 | test_calculator_clicks_and_display_verification | passed | 14510 ms
- 20260905-210103 | test_calculator_division_precision | passed | 9833 ms
- 20260905-210103 | test_notepad_window_identity_observation | passed | 937 ms
- 20260905-210103 | test_notepad_type_semantic_verification | passed | 6862 ms
- 20260905-210103 | test_notepad_moved_window_recovery | passed | 11227 ms
- 20260905-210103 | test_notepad_window_switch_stale_observation | passed | 10601 ms
- 20260905-210436 | test_browser_local_page_window_state_verification | passed | 5888 ms
- 20260905-210436 | test_calculator_clicks_and_display_verification | passed | 15068 ms
- 20260905-210436 | test_calculator_division_precision | passed | 10634 ms
- 20260905-210436 | test_notepad_window_identity_observation | passed | 826 ms
- 20260905-210436 | test_notepad_type_semantic_verification | passed | 6567 ms
- 20260905-210436 | test_notepad_moved_window_recovery | passed | 10895 ms
- 20260905-210436 | test_notepad_window_switch_stale_observation | passed | 10731 ms
- 20260905-210612 | test_browser_local_page_window_state_verification | passed | 4874 ms
- 20260905-210612 | test_calculator_clicks_and_display_verification | passed | 14726 ms
- 20260905-210612 | test_calculator_division_precision | passed | 10311 ms
- 20260905-210612 | test_notepad_window_identity_observation | passed | 1140 ms
- 20260905-210612 | test_notepad_type_semantic_verification | passed | 6878 ms
- 20260905-210612 | test_notepad_moved_window_recovery | passed | 10429 ms
- 20260905-210612 | test_notepad_window_switch_stale_observation | passed | 11703 ms
- 20260905-210928 | test_browser_local_page_window_state_verification | passed | 4751 ms
- 20260905-210928 | test_calculator_clicks_and_display_verification | passed | 14636 ms
- 20260905-210928 | test_calculator_division_precision | passed | 9945 ms
- 20260905-210928 | test_notepad_window_identity_observation | passed | 937 ms
- 20260905-210928 | test_notepad_type_semantic_verification | passed | 6297 ms
- 20260905-210928 | test_notepad_moved_window_recovery | passed | 10499 ms
- 20260905-210928 | test_notepad_window_switch_stale_observation | passed | 10885 ms
- 20260905-213133 | test_browser_local_page_window_state_verification | passed | 5220 ms
- 20260905-213133 | test_calculator_clicks_and_display_verification | passed | 14260 ms
- 20260905-213133 | test_calculator_division_precision | passed | 9556 ms
- 20260905-213133 | test_notepad_window_identity_observation | passed | 813 ms
- 20260905-213133 | test_notepad_type_semantic_verification | passed | 6509 ms
- 20260905-213133 | test_notepad_moved_window_recovery | passed | 10357 ms
- 20260905-213133 | test_notepad_window_switch_stale_observation | passed | 10422 ms
- 20260905-215531 | test_browser_local_page_window_state_verification | passed | 4860 ms
- 20260905-215531 | test_calculator_clicks_and_display_verification | passed | 13067 ms
- 20260905-215531 | test_calculator_division_precision | passed | 9195 ms
- 20260905-215531 | test_notepad_window_identity_observation | passed | 889 ms
- 20260905-215531 | test_notepad_type_semantic_verification | passed | 6284 ms
- 20260905-215531 | test_notepad_moved_window_recovery | passed | 10383 ms
- 20260905-215531 | test_notepad_window_switch_stale_observation | passed | 10039 ms
- 20260905-224338 | test_browser_local_page_window_state_verification | passed | 5108 ms
- 20260905-224338 | test_calculator_clicks_and_display_verification | passed | 14738 ms
- 20260905-224338 | test_calculator_division_precision | passed | 10240 ms
- 20260905-224338 | test_notepad_window_identity_observation | passed | 875 ms
- 20260905-224338 | test_notepad_type_semantic_verification | passed | 6512 ms
- 20260905-224338 | test_notepad_moved_window_recovery | passed | 10804 ms
- 20260905-224338 | test_notepad_window_switch_stale_observation | passed | 10936 ms
- 20260907-181330 | test_browser_local_page_window_state_verification | failed | error: (
            "<!DOCTYPE html><html><head><title>"
            + PAGE_TITLE_MARKER
            + "</title></head><body><h1>E2E target page</h1>"
            "<p>Local verification page for the computer-use-mcp E2E suite.</p></body></html>",
            encoding="utf-8",
        )
        page_url = "file:///" + str(page_path).replace("\\", "/")
>       with edge_app(deadline, page_url) as (proc, hwnd):
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests\e2e\test_e2e_browser.py:91: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
..\.orvex\tools\python\Lib\contextlib.py:137: in __enter__
    return next(self.gen)
           ^^^^^^^^^^^^^^
tests\e2e\test_e2e_browser.py:66: in edge_app
    hwnd = w32.wait_for_window(
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

deadline = <helpers_win32.Deadline object at 0x000001F112C22630>, pid = 13164
class_name = None, title_needle = 'E2E Browser Verification Page 7391'
timeout_s = 45.0

    def wait_for_window(
        deadline: Deadline,
        *,
        pid: int | None = None,
        class_name: str | None = None,
        title_needle: str | None = None,
        timeout_s: float = 30.0,
    ) -> int:
        """Poll for a matching top-level window; raise TimeoutError when none appears."""
        expiry = time.monotonic() + timeout_s
        while time.monotonic() < expiry:
            deadline.check(f"window pid={pid} class={class_name} title~={title_needle}")
            found = find_windows(pid=pid, class_name=class_name, title_needle=title_needle)
            if found:
                return found[0]
            time.sleep(0.25)
>       raise TimeoutError(
            f"No window appeared within {timeout_s}s (pid={pid}, class={class_name}, "
            f"title~={title_needle!r})."
        )
E       TimeoutError: No window appeared within 45.0s (pid=13164, class=None, title~='E2E Browser Verification Page 7391').

tests\e2e\helpers_win32.py:203: TimeoutError | 45184 ms
- 20260907-181330 | test_calculator_clicks_and_display_verification | failed | error: d["7"], expected_effect="calc_display_equals:7",
                                     reason="click digit 7 (grounded from live button grid)"),
                            verification_hint="predicate"),
                    rt.step(rt.click(*grid["*"], expected_effect="calc_display_equals:7",
                                     reason="click multiply — display unchanged; pixel diff cannot verify this"),
                            verification_hint="predicate"),
                    rt.step(rt.click(*grid["6"], expected_effect="calc_display_equals:6",
                                     reason="click digit 6"),
                            verification_hint="predicate"),
                    rt.step(rt.click(*grid["="], expected_effect="calc_display_equals:42",
                                     reason="click equals"),
                            verification_hint="predicate"),
                    rt.done("7*6 computed and verified"),
                ]
            )
            session_id, bundle = make_session(
                provider=provider,
                dry_run=False,
                require_approval=False,
                allowed_processes=["win32calc.exe"],
            )
            with_verifier(session_id, bundle, display_strategy)
            try:
>               observation = observe(evidence, session_id, "before")
                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests\e2e\test_e2e_calculator.py:107: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

evidence = <conftest.Evidence object at 0x000001F112C42F30>
session_id = 'ee45f092aa834d0581a911c67fb36ba7', when = 'before'

    def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
        response = server.computer_observe(session_id)
>       assert response.get("observation_id"), response
               ^^^^^^^^^^^^
E       AttributeError: 'list' object has no attribute 'get'

tests\e2e\test_e2e_calculator.py:60: AttributeError | 935 ms
- 20260907-181330 | test_calculator_division_precision | failed | error: ow(hwnd), "could not focus Calculator"
            grid = w32.calc_button_grid(hwnd)
            display_strategy = rt.CalcDisplayPredicateStrategy(hwnd)
            provider = rt.E2EScriptedProvider(
                [
                    rt.step(rt.click(*grid["1"], expected_effect="calc_display_equals:1",
                                     reason="click digit 1"), verification_hint="predicate"),
                    rt.step(rt.click(*grid["/"], expected_effect="calc_display_equals:1",
                                     reason="click divide (no visual change)"), verification_hint="predicate"),
                    rt.step(rt.click(*grid["8"], expected_effect="calc_display_equals:8",
                                     reason="click digit 8"), verification_hint="predicate"),
                    rt.step(rt.click(*grid["="], expected_effect="calc_display_equals:0.125",
                                     reason="click equals"), verification_hint="predicate"),
                    rt.done("1/8 computed and verified"),
                ]
            )
            session_id, bundle = make_session(
                provider=provider,
                dry_run=False,
                require_approval=False,
                allowed_processes=["win32calc.exe"],
            )
            with_verifier(session_id, bundle, display_strategy)
            try:
>               observe(evidence, session_id, "before")

tests\e2e\test_e2e_calculator.py:222: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

evidence = <conftest.Evidence object at 0x000001F11253D610>
session_id = '7c42dcfe09ed4407bf1e423ab03059ba', when = 'before'

    def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
        response = server.computer_observe(session_id)
>       assert response.get("observation_id"), response
               ^^^^^^^^^^^^
E       AttributeError: 'list' object has no attribute 'get'

tests\e2e\test_e2e_calculator.py:60: AttributeError | 1036 ms
- 20260907-181330 | test_notepad_window_identity_observation | failed | error: deadline = <helpers_win32.Deadline object at 0x000001F11253D460>
e2e_scratch = WindowsPath('C:/Users/localadmin/Desktop/ComputerUse/computer-use-mcp/tests/e2e/scratch/test_notepad_window_identity_observation')
make_session = <function make_session.<locals>.factory at 0x000001F11252FA60>
evidence = <conftest.Evidence object at 0x000001F11253D850>

    def test_notepad_window_identity_observation(
        deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, evidence: Any
    ) -> None:
        """P0-G/I evidence: strong window/process identity + DPI/coordinate-space on real Windows."""
        file_path = e2e_scratch / "identity_probe.txt"
        file_path.write_text("", encoding="utf-8")
        with notepad_app(deadline, file_path) as (proc, hwnd):
            session_id, bundle = make_session()  # real LocalComputerBackend, dry_run default
            try:
                # The hosting console can steal foreground during fixture setup; make the
                # identity observation deterministic by explicitly focusing the target.
                assert w32.focus_window(hwnd), "could not focus Notepad for the identity check"
>               observation = observe(evidence, session_id, "before")
                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests\e2e\test_e2e_notepad.py:100: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

evidence = <conftest.Evidence object at 0x000001F11253D850>
session_id = 'b8bfc8d7a8ae49d4aeb47e63c722f38a', when = 'before'

    def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
        """Capture an observation through the MCP tool and save it as evidence."""
        response = server.computer_observe(session_id)
>       assert response.get("observation_id"), response
               ^^^^^^^^^^^^
E       AttributeError: 'list' object has no attribute 'get'

tests\e2e\test_e2e_notepad.py:60: AttributeError | 521 ms
- 20260907-181330 | test_notepad_type_semantic_verification | failed | error: unction with_verifier.<locals>.inject at 0x000001F112570860>
evidence = <conftest.Evidence object at 0x000001F11257CB90>

    def test_notepad_type_semantic_verification(
        deadline: w32.Deadline, e2e_scratch: Path, make_session: Any, with_verifier: Any, evidence: Any
    ) -> None:
        """P0-A: run_goal types into Notepad; verification reads the REAL Edit control text."""
        file_path = e2e_scratch / "typed.txt"
        file_path.write_text("", encoding="utf-8")
        with notepad_app(deadline, file_path) as (proc, hwnd):
            strategy = rt.WindowTextPredicateStrategy()
            provider = rt.E2EScriptedProvider(
                [
                    # NOTE: no expected_effect — the expected_text intent must carry the typed text.
                    rt.step(rt.type_text(TYPED_MARKER), verification_hint="expected_text"),
                    rt.done("typed and verified"),
                ]
            )
            session_id, bundle = make_session(provider=provider, dry_run=False, require_approval=True)
            with_verifier(session_id, bundle, strategy)
            try:
                assert w32.focus_window(hwnd), "could not focus Notepad before typing"
>               observation = observe(evidence, session_id, "before")
                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests\e2e\test_e2e_notepad.py:143: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

evidence = <conftest.Evidence object at 0x000001F11257CB90>
session_id = '0c5e8ddee7524eb2a8c428e3af4fdbab', when = 'before'

    def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
        """Capture an observation through the MCP tool and save it as evidence."""
        response = server.computer_observe(session_id)
>       assert response.get("observation_id"), response
               ^^^^^^^^^^^^
E       AttributeError: 'list' object has no attribute 'get'

tests\e2e\test_e2e_notepad.py:60: AttributeError | 513 ms
- 20260907-181330 | test_notepad_moved_window_recovery | failed | error:       def fault_move(goal: str, observation: Any, history: list[str]) -> None:
                before = w32.window_rect(hwnd_a)
                w32.move_window(hwnd_a, 1200, 600, 420, 380)
                provider.fault_log.append(
                    {"hook": "move_window", "before": before, "after": w32.window_rect(hwnd_a)}
                )
    
            def redecide(observation: Any) -> dict[str, Any]:
                left, top, width, height = w32.window_rect(hwnd_a)
                return rt.step(
                    rt.click(
                        left + width // 2,
                        top + height // 2,
                        expected_effect="moved.txt",
                        reason="re-grounded click on the moved window",
                    ),
                    verification_hint="window_state",
                )
    
            provider = rt.E2EScriptedProvider(
                [initial_click, redecide, lambda _obs: rt.step(rt.type_text(RECOVERY_MARKER)),
                 rt.done("recovered and typed")],
                hooks=[fault_move, None, None, None],
            )
            session_id, bundle = make_session(provider=provider, dry_run=False, require_approval=False)
            with_verifier(session_id, bundle, strategy)
            try:
>               observe(evidence, session_id, "before")

tests\e2e\test_e2e_notepad.py:246: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

evidence = <conftest.Evidence object at 0x000001F11257DFA0>
session_id = '65ee0c35c0a144599522da5438a499b3', when = 'before'

    def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
        """Capture an observation through the MCP tool and save it as evidence."""
        response = server.computer_observe(session_id)
>       assert response.get("observation_id"), response
               ^^^^^^^^^^^^
E       AttributeError: 'list' object has no attribute 'get'

tests\e2e\test_e2e_notepad.py:60: AttributeError | 1343 ms
- 20260907-181330 | test_notepad_window_switch_stale_observation | failed | error:  "focus_ok": focused})
                left, top, width, height = w32.window_rect(hwnd_a)
                return rt.step(
                    rt.click(left + width // 2, top + height // 2, expected_effect="target_a.txt",
                             reason="re-grounded after staleness rejection"),
                    verification_hint="window_state",
                )
    
            def redecide_again(observation: Any) -> dict[str, Any]:
                left, top, width, height = w32.window_rect(hwnd_a)
                return rt.step(
                    rt.click(left + width // 2, top + height // 2, expected_effect="target_a.txt",
                             reason="re-grounded click, target now focused"),
                    verification_hint="window_state",
                )
    
            provider = rt.E2EScriptedProvider(
                [initial_click, redecide, redecide_again,
                 lambda _obs: rt.step(rt.type_text(STALE_MARKER)),
                 rt.done("typed after staleness recovery")],
                hooks=[fault_switch_window, None, None, None, None],
            )
            session_id, bundle = make_session(provider=provider, dry_run=False, require_approval=False)
            with_verifier(session_id, bundle, strategy)
            try:
>               observe(evidence, session_id, "before")

tests\e2e\test_e2e_notepad.py:367: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

evidence = <conftest.Evidence object at 0x000001F11253D100>
session_id = '24ac73c456eb4b74b0cf0b04f23e6792', when = 'before'

    def observe(evidence: Any, session_id: str, when: str) -> dict[str, Any]:
        """Capture an observation through the MCP tool and save it as evidence."""
        response = server.computer_observe(session_id)
>       assert response.get("observation_id"), response
               ^^^^^^^^^^^^
E       AttributeError: 'list' object has no attribute 'get'

tests\e2e\test_e2e_notepad.py:60: AttributeError | 1061 ms
- 20260907-181832 | test_browser_local_page_window_state_verification | failed | error: (
            "<!DOCTYPE html><html><head><title>"
            + PAGE_TITLE_MARKER
            + "</title></head><body><h1>E2E target page</h1>"
            "<p>Local verification page for the computer-use-mcp E2E suite.</p></body></html>",
            encoding="utf-8",
        )
        page_url = "file:///" + str(page_path).replace("\\", "/")
>       with edge_app(deadline, page_url) as (proc, hwnd):
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^

tests\e2e\test_e2e_browser.py:91: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _
..\.orvex\tools\python\Lib\contextlib.py:137: in __enter__
    return next(self.gen)
           ^^^^^^^^^^^^^^
tests\e2e\test_e2e_browser.py:66: in edge_app
    hwnd = w32.wait_for_window(
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

deadline = <helpers_win32.Deadline object at 0x00000233E480FAA0>, pid = 15708
class_name = None, title_needle = 'E2E Browser Verification Page 7391'
timeout_s = 45.0

    def wait_for_window(
        deadline: Deadline,
        *,
        pid: int | None = None,
        class_name: str | None = None,
        title_needle: str | None = None,
        timeout_s: float = 30.0,
    ) -> int:
        """Poll for a matching top-level window; raise TimeoutError when none appears."""
        expiry = time.monotonic() + timeout_s
        while time.monotonic() < expiry:
            deadline.check(f"window pid={pid} class={class_name} title~={title_needle}")
            found = find_windows(pid=pid, class_name=class_name, title_needle=title_needle)
            if found:
                return found[0]
            time.sleep(0.25)
>       raise TimeoutError(
            f"No window appeared within {timeout_s}s (pid={pid}, class={class_name}, "
            f"title~={title_needle!r})."
        )
E       TimeoutError: No window appeared within 45.0s (pid=15708, class=None, title~='E2E Browser Verification Page 7391').

tests\e2e\helpers_win32.py:227: TimeoutError | 45194 ms
- 20260907-181832 | test_calculator_clicks_and_display_verification | passed | 2971 ms
- 20260907-181832 | test_calculator_division_precision | passed | 1918 ms
- 20260907-181832 | test_notepad_window_identity_observation | passed | 515 ms
- 20260907-181832 | test_notepad_type_semantic_verification | passed | 1058 ms
- 20260907-181832 | test_notepad_moved_window_recovery | passed | 2091 ms
- 20260907-181832 | test_notepad_window_switch_stale_observation | passed | 2724 ms
- 20260907-182201 | test_browser_local_page_window_state_verification | passed | 2005 ms
- 20260907-182216 | test_browser_local_page_window_state_verification | passed | 1920 ms
- 20260907-182216 | test_calculator_clicks_and_display_verification | passed | 2986 ms
- 20260907-182216 | test_calculator_division_precision | passed | 1820 ms
- 20260907-182216 | test_notepad_window_identity_observation | passed | 532 ms
- 20260907-182216 | test_notepad_type_semantic_verification | passed | 1213 ms
- 20260907-182216 | test_notepad_moved_window_recovery | passed | 2030 ms
- 20260907-182216 | test_notepad_window_switch_stale_observation | passed | 2679 ms
- 20260907-185134 | test_browser_local_page_window_state_verification | passed | 1872 ms
- 20260907-185134 | test_calculator_clicks_and_display_verification | passed | 3159 ms
- 20260907-185134 | test_calculator_division_precision | passed | 1883 ms
- 20260907-185134 | test_notepad_window_identity_observation | passed | 573 ms
- 20260907-185134 | test_notepad_type_semantic_verification | passed | 1084 ms
- 20260907-185134 | test_notepad_moved_window_recovery | passed | 2179 ms
- 20260907-185134 | test_notepad_window_switch_stale_observation | passed | 3154 ms

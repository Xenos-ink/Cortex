# Installing and updating Cortex

Ways to install, register, verify, and update the Cortex MCP server. Current
truth; the served tool set the probes below check is the twelve-tool surface
documented in [TOOLS.md](TOOLS.md).

## Requirements

- Windows 10 / 11 / Server with an active, interactive display session — there
  is no headless mode.
- Python 3.11 or newer.
- No vision API key required: the deterministic surface never calls a model.
  The provider is configured only for the optional model-judge verification
  tier (see [CONFIGURATION.md](CONFIGURATION.md)).
- The screen-recording encoder needs `av` (PyAV, constrained `>=18.1.0,<19` in
  `pyproject.toml`). The Windows wheel is about 30 MB because it BUNDLES
  FFmpeg (plus the libx264 / libx265 encoder libraries) — there is no separate
  FFmpeg install. The import is lazy: a missing or broken `av` degrades the
  recording tools' video to the typed `encoder_unavailable` reply (frames,
  timeline and contact sheet still work), never a crash.

### License note (screen recording)

Cortex is MIT. The `av` (PyAV) wheel is BSD-3-Clause. The wheel bundles FFmpeg,
which self-reports LGPL-3.0-or-later (its configure line has
`--enable-version3` and no `--enable-gpl`) together
with the codec libraries libx264 and libx265 (GPL-2.0-or-later); the bundled
avcodec is linked against libx264/libx265, and FFmpeg's own LICENSE.md states
those libraries are GPL and that using them makes the GPL apply to the FFmpeg
build — so for redistribution the av.libs set must be treated as GPL, in
practice GPL-3.0-or-later. Cortex's own MIT source distribution bundles none
of it. They are installed and loaded with `av` whichever encoder runs. Cortex prefers
`h264_mf` (Windows Media Foundation) and runs libx264 only as a fallback;
`CORTEX_RECORD_ENCODER=h264_mf` stops Cortex from ever encoding through
libx264. It does not remove the GPL components from the installation; on
Windows N editions without the Media Feature Pack, video is then unavailable
(`encoder_unavailable`) while frames and the timeline still work. These are
packaging facts, not legal advice. The full third-party licence listing —
every runtime dependency with its licence and the copied licence texts in
`third_party/licenses/` — is in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Manual install

First, a note on the optional pyautogui engine (WP-M); then the two install
routes:

- **Optional pyautogui engine (WP-M):** pyautogui is no longer a default
  dependency (it drags in MouseInfo, PyMsgBox, PyScreeze, PyGetWindow,
  PyTweening, pyperclip and PyRect; Cortex's default SendInput engine never
  imports it). To use the pyautogui input engine (`CORTEX_INPUT_BACKEND=pyautogui`
  or `input_engine='pyautogui'`), install the extra with ONE of:
  `python -m pip install -e ".[pyautogui]"` (from a clone),
  `pip install "computer-use-mcp[pyautogui] @ git+https://github.com/Xenos-ink/Cortex.git"`
  (from git), or `uv sync --extra pyautogui` (with uv). Selecting the engine
  without the extra fails closed with a `ValueError` naming these commands.

- **Clone + editable install (the full route; required for `cortex-mcp install`
  / `update`)**: the CLI's `install`/`update` subcommands manage a repository
  clone with an editable install — the repo root is resolved as the parent of
  the package's directory (cli.py `default_repo()`), which is a real clone only
  for an editable install — and `update` needs `git` and an `origin/main`
  remote in that clone. From git:

  ```bash
  git clone https://github.com/Xenos-ink/Cortex.git
  cd Cortex
  py -3.11 -m venv .venv
  .\.venv\Scripts\Activate.ps1        # Git Bash / WSL: source .venv/Scripts/activate
  python -m pip install -e .
  ```

  `.[dev]` is for contributors only (tests; it includes pyautogui) —
  `python -m pip install -e ".[dev]"`.

- **Package only (`pip install git+…`)**: installs the server so an MCP client
  can run it, but `cortex-mcp install`/`update` will not find a clone to manage
  (their repo root resolves to the parent of the package's directory — a real
  clone only for an editable install) and `update`
  needs a clone to fast-forward. Use this only when you register the server by
  hand:

  ```bash
  pip install git+https://github.com/Xenos-ink/Cortex.git
  ```

The package installs three console scripts — `cortex` and `computer-use-mcp`
(both start the stdio MCP server) and `cortex-mcp` (the CLI below). Running the
server blocks while it waits for MCP traffic on stdin — that is what a stdio
server does; configure it in an MCP client instead of running it interactively.

Updating:

```bash
pip install --upgrade git+https://github.com/Xenos-ink/Cortex.git
# or, from a clone:
git pull && python -m pip install -e .
```

Upgrade note (WP-M): pip never removes dependencies dropped from the
requirements, so an install upgraded from 0.7.6 or earlier keeps the old
pyautogui family on disk. To remove it (SKIP this if you use
`CORTEX_INPUT_BACKEND=pyautogui`):

```bash
python -m pip uninstall -y pyautogui mouseinfo pymsgbox pyscreeze pygetwindow pytweening pyperclip pyrect
```

For uv-managed installs, `uv sync --frozen` instead resolves the environment
to exactly the locked set and drops the family on the next sync — pyautogui-
engine users must run `uv sync --frozen --extra pyautogui`, and contributors
`uv sync --frozen --extra dev`.

## Running the server

```bash
python -m computer_use_mcp.server     # or just: cortex
```

MCP over stdio: stdout carries the protocol, logs go to stderr (logger name
`computer_use_mcp.server`, level from `LOG_LEVEL`), and per-session audit logs
are written under `%TEMP%\cortex\logs` (override with
`COMPUTER_USE_MCP_LOG_DIR`).

A generic client configuration block (the same shape works in Claude-class,
ZCode-class and Cursor-class clients; adjust paths):

```json
{
  "mcpServers": {
    "cortex": {
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["-m", "computer_use_mcp.server"],
      "cwd": "C:\\path\\to\\Cortex",
      "env": {}
    }
  }
}
```

The `env` block is optional. Set `"CORTEX_IMAGE_DELIVERY": "text"` there when
the client's default model cannot view images (recommended — see
[CONFIGURATION.md](CONFIGURATION.md)).

## The `cortex-mcp` CLI

A global console script (equivalently `python -m computer_use_mcp.cli`) with
four subcommands:

```bash
cortex-mcp install        [--repo PATH] [--venv PATH] [--agents a,b|all] [--force] [--dry-run]
cortex-mcp update         [--repo PATH] [--dry-run]
cortex-mcp uninstall-ocr  [--repo PATH] [--venv PATH] [--dry-run]
cortex-mcp probe          [--python PATH] [--cwd PATH] [--timeout S]
```

- `install` provisions `<repo>/.venv` (editable install), registers the Cortex
  MCP server into every recognized agent config file it finds (zcode, claude,
  cursor, codex, kimi), and verifies the built server with a zero-input stdio
  probe (exactly the registered tool surface, zero `anyOf`/`$ref`).
- Guarantees: every config file is backed up before the first write
  (`<file>.cortex-backup-<timestamp>`), and an existing cortex entry is never
  overwritten — entries that already match are left untouched (`UNCHANGED`),
  entries that differ are reported and skipped
  (`SKIPPED-EXISTS-USE-FORCE`) unless `--force`. Manual config editing remains
  fully supported.
- `update` fast-forwards the repo clone to `origin/main` (it needs `git` and
  the remote; it aborts cleanly when the branch is not fast-forwardable and
  never resets), refreshes the editable installs, verifies the package version
  before/after, and re-probes.
- `probe` is verification only: starts the server once over stdio (default:
  this repo's `.venv` python, falling back to the running interpreter) and
  prints a single `PROBE PASS` (exit 0) or `PROBE FAIL` line.
- `uninstall-ocr` removes the optional `cortex_text_ocr` side package from the
  managed venv (a no-op when not installed); the UIA substrate remains the
  default either way.
- At the natural completion point of `install`/`update` the CLI offers the
  optional OCR text substrate (`cortex_text_ocr`) interactively, DEFAULT NO;
  non-interactive runs always skip (the printed hint shows the manual pip
  command). Installing it is acceptance of the added per-observation OCR
  latency; `uninstall-ocr` removes it. See below.
- The global `cortex-mcp` command is installed best-effort and may require an
  elevated terminal; without it, use the fallback
  `python -m computer_use_mcp.cli`.

## Installing via an AI agent

You do not have to do any of this by hand. Copy one of the two prompts below
into any MCP-capable agent and it will install or update Cortex for itself end
to end. The canonical install location is `%LOCALAPPDATA%\Cortex` on Windows
(`~/.local/share/cortex` elsewhere), and verification is one command:
`"<venv python>" -m computer_use_mcp.cli probe`.

### Install prompt

```text
You are asked to make the Cortex MCP server available to yourself (this agent). Follow exactly; do not skip the CHECK.

CONSTANTS (resolve them once and substitute everywhere below):
- CANONICAL DIR (where the repo clone and its .venv live):
    Windows:  %LOCALAPPDATA%\Cortex
    Other OS: ~/.local/share/cortex
  <canonical> below means this directory for your platform; <venv python> means
  <canonical>\.venv\Scripts\python.exe on Windows or <canonical>/.venv/bin/python otherwise.
- REPO URL: https://github.com/Xenos-ink/Cortex.git
- REGISTRATION BLOCK (what you will add to YOUR OWN MCP config in STEP 4):
    command = "<venv python>"
    args = ["-m", "computer_use_mcp.server"]
    cwd = "<canonical>"        (include it only when your config format supports a cwd field)

STEP 1 — CHECK (do this before anything else):
  a) Does <canonical> with its .venv already exist, and does the probe pass? Run:
       "<venv python>" -m computer_use_mcp.cli probe
     PASS prints one line starting with "PROBE PASS" and exits 0.
  b) Get the installed version (this is the X for the report below). Run:
       "<venv python>" -c "import computer_use_mcp; print(computer_use_mcp.__version__)"
  c) Does YOUR OWN MCP config already contain a "cortex" entry whose command points at
     the canonical <venv python>?
  If a) and c) are both true: report "already installed and registered (version X)" and
  STOP. Do not reinstall and do not touch any config file.

STEP 2 — GET THE CODE into the canonical dir:
  - <canonical> missing: create it by cloning:
      git clone https://github.com/Xenos-ink/Cortex.git "<canonical>"
  - <canonical> already present: bring it up to date, fast-forward only:
      git -C "<canonical>" fetch origin
      git -C "<canonical>" merge --ff-only origin/main
    If that merge is not a fast-forward: STOP and tell the user. NEVER run git reset or
    any --hard command in the canonical dir.

STEP 3 — INSTALL (inside the canonical clone):
  - If <canonical>/.venv is missing, create it (from inside <canonical>):
      python -m venv .venv
  - Refresh the editable install:
      "<venv python>" -m pip install -e "<canonical>"
  - Verify with the probe from STEP 1a. It MUST print PASS and list exactly these 12 tools:
      start_session, stop_session, computer_observe, computer_screenshot,
      computer_execute, computer_zoom, computer_wait, computer_cancel_wait,
      computer_wait_status, computer_fetch_image, computer_record,
      computer_record_result
    If it prints FAIL: report the printed reason to the user and stop.

STEP 4 — REGISTER THE SERVER FOR YOURSELF (your own config, not anyone else's):
  Add the REGISTRATION BLOCK to YOUR OWN MCP config file, in your own format, at your
  preferred scope. Rules:
  - Back up your config file before you edit it (keep the backup next to it).
  - If a "cortex" entry already exists in it: do NOT overwrite it. Compare it with the
    REGISTRATION BLOCK, report the differences to the user, and ask how to proceed.
  Common config locations and shapes, if your agent is one of these:
  - ZCode: %USERPROFILE%\.zcode\cli\config.json — the key there is "mcp.servers" (NOT
    "mcpServers"); compatible fallback: ~\.agents\mcp.json in the "mcpServers" shape.
  - Claude Desktop: %APPDATA%\Claude\claude_desktop_config.json ("mcpServers" shape).
  - Cursor: %USERPROFILE%\.cursor\mcp.json ("mcpServers" shape).
  - Project-level, inside a project folder: <project>\.zcode\config.json,
    <project>\.cursor\mcp.json, <project>\.mcp.json ("mcpServers" shape).
  - JSON rules: double quotes only, and double the backslashes in Windows paths ("\\").

STEP 5 — REPORT to the user:
  - the installed version,
  - the canonical dir,
  - the config file you edited (full path),
  - the probe result (the PASS line),
  - and: restart yourself / your host to load the newly registered server.
```

### Update prompt

```text
You are asked to update the Cortex MCP server installation for yourself. Follow exactly; never force.

CONSTANTS (identical to the install prompt; resolve once, substitute everywhere):
- CANONICAL DIR:  Windows: %LOCALAPPDATA%\Cortex   Other OS: ~/.local/share/cortex
  <canonical> means this directory for your platform; <venv python> means
  <canonical>\.venv\Scripts\python.exe on Windows or <canonical>/.venv/bin/python otherwise.
- REPO URL: https://github.com/Xenos-ink/Cortex.git
- REGISTRATION BLOCK (for reference only — an update does NOT change it):
    command = "<venv python>"
    args = ["-m", "computer_use_mcp.server"]
    cwd = "<canonical>"        (when the config format supports it)

STEP 1 — LOCATE: if <canonical> (or its .venv) is missing there is nothing to update:
  follow the Install prompt steps instead, and stop after its STEP 5.

STEP 2 — RECORD THE CURRENT STATE, THEN FAST-FORWARD:
  - Record the version BEFORE. Run:
      "<venv python>" -m computer_use_mcp.cli probe
      "<venv python>" -c "import computer_use_mcp; print(computer_use_mcp.__version__)"
  - Bring the clone up to date, fast-forward only:
      git -C "<canonical>" fetch origin
      git -C "<canonical>" merge --ff-only origin/main
    If that merge is not a fast-forward: STOP and tell the user. NEVER run git reset or
    any --hard command in the canonical dir.

STEP 3 — REFRESH AND VERIFY:
  - Refresh the editable install:
      "<venv python>" -m pip install -e "<canonical>"
  - Re-run the probe. It MUST print PASS (exactly the 12 tools: start_session,
    stop_session, computer_observe, computer_screenshot, computer_execute,
    computer_zoom, computer_wait, computer_cancel_wait, computer_wait_status,
    computer_fetch_image, computer_record, computer_record_result). If it
    prints FAIL: report the printed reason and stop.

STEP 4 — REPORT to the user:
  - version BEFORE -> version AFTER (re-run the version command if needed),
  - the probe result (the PASS line),
  - a note that the MCP config entry needs NO change (the registration block keeps the
    same command path),
  - and: restart yourself / your host to load the update.
```

## The optional OCR side package

The spatial-text seam auto-detects `cortex_text_ocr` (an optional Windows
Media OCR substrate shipped in-repo under `sidepackages/`). It is never
installed by default; `install`/`update` OFFER it interactively with default
No, and non-interactive runs always skip — installing it is the user's
acceptance of the added per-observation OCR latency (reported as
`substrate_ms`). Manual install:
`"<venv python>" -m pip install <repo>/sidepackages/cortex_text_ocr`.
`cortex-mcp uninstall-ocr` removes it. UIA remains the default and fallback
substrate either way.

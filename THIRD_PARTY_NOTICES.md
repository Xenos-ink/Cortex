# Third-party notices

Cortex is MIT-licensed (see [LICENSE](LICENSE)). The third-party dependencies
listed below are **not** bundled in this repository or in its GitHub releases:
the installers (`python -m pip install -e .` from a clone, or
`pip install git+…`) fetch them from PyPI at install time and resolve versions
freshly within the ranges in `pyproject.toml`; the versions below are the
locked, tested set from `uv.lock` (`uv sync --frozen` installs exactly this
set). The first table covers the **default runtime dependency closure** of the
Windows install (direct and transitive). The second table covers the
**optional extra `pyautogui`**, needed only for the pyautogui input engine
(`CORTEX_INPUT_BACKEND=pyautogui` or `input_engine='pyautogui'`); it is not
installed by default. Licence texts copied from the installed distributions
live in [`third_party/licenses/`](third_party/licenses/), and standard SPDX
licence texts for the libraries bundled inside the av wheel in
[`third_party/licenses/spdx/`](third_party/licenses/spdx/).

This file is informational and is not legal advice.

## Runtime dependencies (default install)

| Name | Version | Licence (declared) | Upstream | Licence files in third_party/licenses/ | Pulled in by |
|---|---|---|---|---|---|
| av | 18.1.0 | BSD-3-Clause | https://pyav.basswood.io | av-LICENSE.txt, av-AUTHORS.rst | computer-use-mcp (direct) |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx | httpx-LICENSE.md | computer-use-mcp (direct) |
| mcp | 1.30.0 | MIT | https://modelcontextprotocol.io | mcp-LICENSE.txt | computer-use-mcp (direct) |
| mss | 10.2.0 | MIT | https://github.com/BoboTiG/python-mss | mss-LICENSE.txt | computer-use-mcp (direct) |
| pillow | 12.3.0 | MIT-CMU | https://python-pillow.github.io | pillow-LICENSE | computer-use-mcp (direct) |
| pydantic | 2.13.5 | MIT | https://github.com/pydantic/pydantic | pydantic-LICENSE | computer-use-mcp (direct) |
| pydantic-settings | 2.15.0 | MIT | https://github.com/pydantic/pydantic-settings | pydantic_settings-LICENSE | computer-use-mcp (direct) |
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types | annotated_types-LICENSE | pydantic |
| anyio | 4.15.1 | MIT | https://github.com/agronholm/anyio | anyio-LICENSE | httpx, mcp, starlette, sse-starlette |
| attrs | 26.1.0 | MIT | https://www.attrs.org | attrs-LICENSE | jsonschema, referencing |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi | certifi-LICENSE | httpx, httpcore |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi | cffi-LICENSE | cryptography |
| click | 8.5.0 | BSD-3-Clause | https://palletsprojects.com/p/click/ | click-LICENSE.txt | uvicorn |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography | cryptography-LICENSE, cryptography-LICENSE.APACHE, cryptography-LICENSE.BSD | pyjwt (crypto extra, via mcp) |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 | h11-LICENSE.txt | httpcore, uvicorn |
| httpcore | 1.0.9 | BSD-3-Clause | https://www.encode.io/httpcore/ | httpcore-LICENSE.md | httpx |
| httpx-sse | 0.4.3 | MIT | https://github.com/florimondmanca/httpx-sse | httpx_sse-LICENSE | mcp |
| idna | 3.19 | BSD-3-Clause | https://github.com/kjd/idna | idna-LICENSE.md | anyio, httpx |
| jsonschema | 4.26.0 | MIT | https://github.com/python-jsonschema/jsonschema | jsonschema-COPYING | mcp |
| jsonschema-specifications | 2025.9.1 | MIT | https://github.com/python-jsonschema/jsonschema-specifications | jsonschema_specifications-COPYING | jsonschema |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser | pycparser-LICENSE | cffi |
| pydantic-core | 2.46.5 | MIT | https://github.com/pydantic/pydantic | pydantic_core-LICENSE | pydantic |
| pyjwt | 2.13.0 | MIT | https://github.com/jpadilla/pyjwt | pyjwt-LICENSE, pyjwt-AUTHORS.rst | mcp (crypto extra) |
| python-dotenv | 1.2.3 | BSD-3-Clause | https://github.com/theskumar/python-dotenv | python_dotenv-LICENSE | pydantic-settings |
| python-multipart | 0.0.32 | Apache-2.0 | https://github.com/Kludex/python-multipart | python_multipart-LICENSE.txt | mcp |
| pywin32 | 312 | PSF | https://github.com/mhammond/pywin32 | pywin32-win32-License.txt, pywin32-pythonwin-License.txt, pywin32-adodbapi-license.txt, pywin32-com-License.txt, pywin32-idle-License.txt, pywin32-Scintilla-License.txt, pywin32-MAPIStubLibrary-License, pywin32-isapi-README.txt | mcp (Windows) |
| referencing | 0.37.0 | MIT | https://github.com/python-jsonschema/referencing | referencing-COPYING | jsonschema, jsonschema-specifications |
| rpds-py | 2026.6.3 | MIT | https://github.com/crate-py/rpds | rpds_py-LICENSE | jsonschema, referencing |
| sse-starlette | 3.4.11 | BSD-3-Clause | https://github.com/sysid/sse-starlette | sse_starlette-LICENSE, sse_starlette-AUTHORS | mcp |
| starlette | 1.6.0 | BSD-3-Clause | https://github.com/Kludex/starlette | starlette-LICENSE.md | mcp, sse-starlette |
| typing-extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions | typing_extensions-LICENSE | anyio, mcp, pydantic, pydantic-core, referencing, starlette, typing-inspection |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection | typing_inspection-LICENSE | mcp, pydantic, pydantic-settings |
| uvicorn | 0.52.4 | BSD-3-Clause | https://uvicorn.dev/ | uvicorn-LICENSE.md | mcp |

Notes for this table:

- **pillow**: its shipped `pillow-LICENSE` already contains the licences of the
  libraries Pillow bundles, including FreeType's FTL/GPL-2.0 dual licence and
  libjpeg's IJG licence; the file is the licence of record for those.
- **cryptography**: the wheel statically bundles OpenSSL (Apache-2.0). The
  wheel itself ships SBOM documents
  (`cryptography-50.0.1.dist-info/sboms/`) listing the bundled components;
  its own code is licensed Apache-2.0 OR BSD-3-Clause
  (`cryptography-LICENSE.APACHE`, `cryptography-LICENSE.BSD`). The standard
  Apache-2.0 text is in `spdx/Apache-2.0.txt`.
- **pywin32**: `pywin32-isapi-README.txt` is a declared License-File
  (`isapi/README.txt`) carrying a separate copyright (Blackdog Software Pty
  Ltd) inside the PSF-licensed distribution.

## Optional extra `pyautogui`

The pyautogui input engine (`CORTEX_INPUT_BACKEND=pyautogui` or
`input_engine='pyautogui'`) needs the extra. Install it with ONE of:

- from a clone: `python -m pip install -e ".[pyautogui]"`
- from git: `pip install "computer-use-mcp[pyautogui] @ git+https://github.com/Xenos-ink/Cortex.git"`
- with uv: `uv sync --extra pyautogui`

Cortex itself never imports these packages on the default SendInput engine;
they arrive only through pyautogui.

| Name | Version | Licence (declared) | Upstream | Licence files in third_party/licenses/ | Pulled in by |
|---|---|---|---|---|---|
| pyautogui | 0.9.54 | BSD | https://github.com/asweigart/pyautogui | pyautogui-LICENSE.txt, pyautogui-AUTHORS.txt | computer-use-mcp (extra `pyautogui`, `dev`) |
| mouseinfo | 0.1.3 | GPLv3+ | https://github.com/asweigart/mouseinfo | none shipped | pyautogui |
| pygetwindow | 0.0.9 | BSD | https://github.com/asweigart/pygetwindow | none shipped | pyautogui |
| pymsgbox | 2.0.1 | BSD-3-Clause (shipped licence text); PyPI metadata classifier says GPLv3+ — see note below | https://github.com/asweigart/pymsgbox | pymsgbox-LICENSE.txt, pymsgbox-AUTHORS.txt | pyautogui |
| pyperclip | 1.11.0 | BSD | https://github.com/asweigart/pyperclip | pyperclip-LICENSE.txt, pyperclip-AUTHORS.txt | mouseinfo |
| pyrect | 0.2.0 | BSD | https://github.com/asweigart/pyrect | pyrect-LICENSE.txt, pyrect-AUTHORS.txt | pygetwindow |
| pyscreeze | 1.0.1 | MIT | https://github.com/asweigart/pyscreeze | pyscreeze-LICENSE.txt, pyscreeze-AUTHORS.txt | pyautogui |
| pytweening | 1.2.0 | MIT | https://github.com/asweigart/pytweening | pytweening-LICENSE.txt, pytweening-AUTHORS.txt | pyautogui |

Notes for this table:

- **MouseInfo — GPLv3+.** Declared "GPLv3+" in its PyPI metadata. It is
  published only as an sdist, so the wheel uv/pip builds locally contains no
  licence file; the declared licence is named per its PyPI metadata. pyautogui
  imports it at module import time (`pyautogui/__init__.py`), so selecting the
  pyautogui engine imports it. Importing and using a GPLv3+ module from an MIT
  application does not place any obligation on Cortex's own source
  distribution; obligations arise only when the combined work is conveyed (see
  Compatibility below).
- **PyMsgBox — metadata mismatch.** Its PyPI metadata classifier says "GPLv3+",
  but the `LICENSE.txt` shipped inside its wheel is a BSD-3-Clause text
  ("Copyright (c) 2014, Al Sweigart"). This is an upstream packaging
  inconsistency; this notice treats the shipped licence text as the licence.
- **No licence file in the locally built wheel:** MouseInfo 0.1.3 (declared
  "GPLv3+" per its PyPI metadata, upstream
  https://github.com/asweigart/mouseinfo) and PyGetWindow 0.0.9 (declared
  "BSD" classifier, upstream https://github.com/asweigart/pygetwindow) are
  sdist-only upstream; the wheel pip builds locally contains no licence file.
  Their licence texts were not invented or downloaded; the declared licence is
  named honestly.

## Libraries bundled inside the av wheel (av.libs)

The `av` wheel itself is BSD-3-Clause (`av-LICENSE.txt`). Its Windows wheel
also bundles the following shared libraries in `site-packages/av.libs`, which
are installed and loaded with `av`. FFmpeg self-reports
`avcodec_license()` = "LGPL version 3 or later" and its build configure line
(from `avcodec_configuration()`, verbatim) contains `--enable-version3` and
**no** `--enable-gpl`. However, the bundled avcodec is **linked against
libx264 and libx265 (GPL-2.0-or-later)** — the binary carries
`--enable-libx264` and imports `x264_encoder_open` — and FFmpeg's own
LICENSE.md states that libx264/libx265 are GPL and that combining FFmpeg with
them makes the GPL apply to the FFmpeg build. **For redistribution, the
av.libs set must therefore be treated as GPL — in practice
GPL-3.0-or-later**, given `--enable-version3` and the Apache-2.0 opencore-amr.
The "LGPL version 3 or later" self-report reflects the build flags, not the
licence of the combined binaries. Cortex's own MIT source distribution is
unaffected (it bundles none of these libraries). Both Cortex encoders stay
available by design: `h264_mf` (Windows Media Foundation) is preferred,
`libx264` is the fallback, and `CORTEX_RECORD_ENCODER=h264_mf` keeps Cortex
itself from ever encoding through libx264 — but the DLLs are present and
loaded with `av` whichever encoder runs. Except where the evidence column says
otherwise, these licence statements are per the upstream projects and are NOT
shipped in the wheel.

| DLL file(s) in av.libs | Library | Licence | Upstream | Evidence |
|---|---|---|---|---|
| avcodec-62-*.dll, avdevice-62-*.dll, avfilter-11-*.dll, avformat-62-*.dll, avutil-60-*.dll, swresample-6-*.dll, swscale-9-*.dll | FFmpeg 8.1.2 (shared libraries) | build self-reports LGPL-3.0-or-later; linked against GPL libx264/libx265, so the combined set must be treated as GPL (in practice GPL-3.0-or-later) for redistribution | https://ffmpeg.org | the library's own runtime self-report (`avcodec_license()`) and configure line (`avcodec_configuration()`: `--enable-version3`, no `--enable-gpl`, `--enable-libx264`); FFmpeg's LICENSE.md on the GPL combination — spdx/LGPL-3.0-or-later.txt, spdx/GPL-3.0-or-later.txt |
| (no separate DLL; headers compiled into avcodec) | NVIDIA NVENC and AMD AMF headers | MIT | https://github.com/NVIDIA/nv-codec-headers , https://github.com/GPUOpen-LibrariesAndSDKs/AMF | per upstream project, not shipped in the wheel — spdx/MIT.txt |
| libx264-165-*.dll | x264 (H.264 encoder) | GPL-2.0-or-later | https://www.videolan.org/developers/x264.html | per upstream project, not shipped in the wheel — spdx/GPL-2.0-or-later.txt |
| libx265-*.dll | x265 (HEVC encoder) | GPL-2.0-or-later | https://bitbucket.org/multicoreware/x265_git | per upstream project, not shipped in the wheel — spdx/GPL-2.0-or-later.txt |
| libdav1d-*.dll | dav1d (AV1 decoder) | BSD-2-Clause | https://code.videolan.org/videolan/dav1d | per upstream project, not shipped in the wheel — spdx/BSD-2-Clause.txt |
| libvpx-1-*.dll | libvpx (VP8/VP9) | BSD-3-Clause, plus a royalty-free patent grant (source PATENTS file) | https://chromium.googlesource.com/webm/libvpx | per upstream project, not shipped in the wheel — spdx/BSD-3-Clause.txt |
| libopus-0-*.dll | Opus | BSD-3-Clause | https://opus-codec.org | per upstream project, not shipped in the wheel — spdx/BSD-3-Clause.txt |
| libwebp-*.dll, libsharpyuv-*.dll, libwebpmux-*.dll | libwebp / libsharpyuv / libwebpmux | BSD-3-Clause, plus a patent grant (source PATENTS file) | https://developers.google.com/speed/webp | per upstream project, not shipped in the wheel — spdx/BSD-3-Clause.txt |
| libmp3lame-0-*.dll | LAME (MP3 encoder) | LGPL-2.0-or-later | https://lame.sourceforge.io | per upstream project, not shipped in the wheel — spdx/LGPL-2.0-or-later.txt |
| libopencore-amrnb-0-*.dll, libopencore-amrwb-0-*.dll | OpenCORE AMR | Apache-2.0 | https://sourceforge.net/projects/opencore-amr/ | per upstream project, not shipped in the wheel — spdx/Apache-2.0.txt |
| libSvtAv1Enc-*.dll | SVT-AV1 (AV1 encoder) | BSD-3-Clause-Clear plus the AOMedia Patent License 1.0 | https://gitlab.com/AOMediaCodec/SVT-AV1 | per upstream project, not shipped in the wheel — spdx/BSD-3-Clause-Clear.txt |
| libvpl-*.dll | Intel VPL (oneVPL) | MIT | https://github.com/intel/libvpl | per upstream project, not shipped in the wheel — spdx/MIT.txt |
| zlib1-*.dll | zlib | Zlib | https://zlib.net | per upstream project, not shipped in the wheel — spdx/Zlib.txt |
| libiconv-2-*.dll | GNU libiconv | LGPL-2.1-or-later | https://www.gnu.org/software/libiconv/ | per upstream project, not shipped in the wheel — spdx/LGPL-2.1-or-later.txt |
| libgcc_s_seh-1-*.dll, libstdc++-6-*.dll | GCC runtime (libgcc, libstdc++) | GPL-3.0-or-later WITH GCC-exception-3.1 | https://gcc.gnu.org | per upstream project, not shipped in the wheel — spdx/GPL-3.0-or-later.txt + spdx/GCC-exception-3.1.txt |
| libwinpthread-1-*.dll | MinGW-w64 winpthreads | MIT AND BSD-3-Clause (verify) | https://sourceforge.net/projects/mingw-w64/ | per upstream project, not shipped in the wheel — spdx/MIT.txt + spdx/BSD-3-Clause.txt |

## Copyleft and special cases

### (a) certifi — MPL-2.0

`certifi` is used unmodified, as distributed by the installer from PyPI.
MPL-2.0 is file-level copyleft: it applies to certifi's own source files, and
a redistributor of a bundled install must provide certifi's source under
MPL-2.0.

### (b) The GPL components in the default install

The default install includes the av wheel's bundled GPL-2.0-or-later
libx264/libx265 and the GCC runtime (GPL-3.0-or-later WITH GCC-exception-3.1).
Cortex's own repository and releases contain none of them. MouseInfo arrives
only with the `pyautogui` extra. As set out in the av.libs table above, the
av.libs set (FFmpeg linked against libx264/libx265) must be treated as GPL —
in practice GPL-3.0-or-later — for redistribution, even though FFmpeg's build
flags self-report LGPL-3.0-or-later. The av wheel ships neither the source of
these libraries nor a written offer for it; a redistributor of a bundled
install must supply the FFmpeg/x264/x265 corresponding source themselves.
Cortex's default encoder is `h264_mf`; libx264 runs only as the fallback or
when explicitly selected, and the FFmpeg libraries are used by `av` for
demuxing/muxing whichever encoder runs.

### (c) The pyautogui extra (off by default)

MouseInfo (GPLv3+) is only reachable through the optional extra `pyautogui`;
see the optional-extra table above.

## Compatibility

MIT (Cortex's licence) is compatible with GPLv3 and with
GPL-2.0-or-later, and with Apache-2.0, BSD-2/BSD-3-Clause, MPL-2.0, PSF /
PSF-2.0, MIT-0, MIT-CMU and LGPL for this use. Cortex's GitHub distribution
contains only its own MIT source; the dependencies are fetched from PyPI at
install time under their own licences. Cortex's own source distribution is
unaffected by the copyleft components above. Anyone who **redistributes a
bundled install** — a zipped virtual environment, an installer, a frozen
executable — must meet each included component's terms: for the GPL
components (the av.libs set treated as GPL — in practice GPL-3.0-or-later —
because avcodec is linked against GPL libx264/libx265; MouseInfo when the
`pyautogui` extra is bundled) that means providing the corresponding source —
which the av wheel does not include and does not offer, so the redistributor
must supply the FFmpeg/x264/x265 source themselves — and the licence text;
for LGPL components (libmp3lame, libiconv in the av.libs set), providing the
corresponding source of those libraries and the licence text; for the MPL-2.0
component (certifi), its source files under MPL-2.0; for Apache-2.0
components, the licence text plus any NOTICE; for BSD/MIT components, the
copyright and licence notices. This file is informational and is not legal
advice.

## Dev-only tools (not installed for users, not distributed)

`pytest` (MIT), `pytest-asyncio` (Apache-2.0) and `ruff` (MIT) are the `dev`
optional dependency group of `computer-use-mcp`, along with its `pyautogui`
pin — which exists so the contributors' test suite keeps covering the
pyautogui input engine. `.[dev]` is for contributors only: the end-user
install routes (`pip install -e .`) do not install the dev group. The dev
tools are not part of any distributed release.

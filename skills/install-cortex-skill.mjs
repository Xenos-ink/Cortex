#!/usr/bin/env node
// install-cortex-skill.mjs — side-install the cortex-fast agent skill.
//
// Stdlib-only Node >=18 ESM script. Copies the bundled skill (skills/cortex-fast/,
// resolved relative to this file so it works under `npx` straight from the git
// checkout) into one or more agent skill directories.
//
// Status lines follow the repo's cortex-mcp CLI conventions:
//   [cortex-fast] <path>: INSTALLED
//   [cortex-fast] <path>: UNCHANGED (identical content)
//   [cortex-fast] <path>: SKIPPED-EXISTS-USE-FORCE (differing content)
// An existing skill dir is never clobbered: --force first moves it aside to
// <name>.cortex-backup-<YYYYmmdd-HHMMSS> (an existing backup is never overwritten).
//
// No telemetry, no network calls.

import { readdir, readFile, mkdir, cp, rename } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const SKILL_NAME = "cortex-fast";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SKILL_SRC = path.join(HERE, SKILL_NAME);

const USER_SCOPE = [".zcode", ".agents", ".claude"].map((d) =>
  path.join(os.homedir(), d, "skills")
);
const PROJECT_SCOPE = [".zcode", ".agents", ".claude"].map((d) =>
  path.join(process.cwd(), d, "skills")
);

const INSTALLED = "INSTALLED";
const UNCHANGED = "UNCHANGED";
const SKIPPED = "SKIPPED-EXISTS-USE-FORCE";

const USAGE = `Install the ${SKILL_NAME} agent skill (fast-path driving for the Cortex computer-use MCP server).

Usage:
  node install-cortex-skill.mjs [flags]

Targets (default): every EXISTING user-scope skill directory among
  ~/.zcode/skills, ~/.agents/skills, ~/.claude/skills

Flags:
  --dir <path>  install into <path>/${SKILL_NAME} (created if missing); overrides detection
  --all         also target project-scope ./.zcode/skills, ./.agents/skills, ./.claude/skills
                when they exist
  --force       overwrite a differing existing skill dir (backs it up first to
                ${SKILL_NAME}.cortex-backup-<YYYYmmdd-HHMMSS>; never overwrites a backup)
  --list        print detected targets and the plan; no writes
  --help        this text

Exit codes: 0 if anything was installed or was already identical; 1 if nothing was.
`;

function parseArgs(argv) {
  const opts = { dir: null, all: false, force: false, list: false, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--help" || a === "-h") opts.help = true;
    else if (a === "--list") opts.list = true;
    else if (a === "--force") opts.force = true;
    else if (a === "--all") opts.all = true;
    else if (a === "--dir") {
      const v = argv[++i];
      if (!v || v.startsWith("--")) {
        console.error("error: --dir requires a path argument");
        process.exit(2);
      }
      opts.dir = path.resolve(v);
    } else {
      console.error(`error: unknown argument: ${a} (try --help)`);
      process.exit(2);
    }
  }
  return opts;
}

async function exists(p) {
  try {
    await readdir(p);
    return true;
  } catch {
    return false;
  }
}

/** Map of relativePath -> Buffer for every file under dir. */
async function snapshot(dir) {
  const out = new Map();
  async function walk(rel) {
    const entries = await readdir(path.join(dir, rel), { withFileTypes: true });
    for (const e of entries) {
      const r = path.join(rel, e.name);
      if (e.isDirectory()) await walk(r);
      else out.set(r.split(path.sep).join("/"), await readFile(path.join(dir, r)));
    }
  }
  await walk("");
  return out;
}

function timestamp(d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}` +
    `-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
  );
}

async function backupDir(dir) {
  const parent = path.dirname(dir);
  const base = path.basename(dir);
  let target = path.join(parent, `${base}.cortex-backup-${timestamp()}`);
  for (let n = 2; await exists(target); n++) {
    target = path.join(parent, `${base}.cortex-backup-${timestamp()}-${n}`);
  }
  await rename(dir, target);
  return target;
}

async function classify(dest) {
  const src = await snapshot(SKILL_SRC);
  let dst;
  try {
    dst = await snapshot(dest);
  } catch {
    return [INSTALLED, "target created"];
  }
  for (const [rel, buf] of src) {
    const other = dst.get(rel);
    if (other === undefined) return [INSTALLED, `missing file: ${rel}`];
    if (!buf.equals(other)) return [SKIPPED, `differing content: ${rel}`];
  }
  for (const rel of dst.keys()) {
    if (!src.has(rel)) return [SKIPPED, `extra file: ${rel}`];
  }
  return [UNCHANGED, "identical content"];
}

async function installInto(dest, opts) {
  const label = path.join(dest, SKILL_NAME);
  const [status, detail] = await classify(label);
  if (status === UNCHANGED) {
    console.log(`[${SKILL_NAME}] ${label}: ${UNCHANGED} (${detail})`);
    return status;
  }
  if (status === SKIPPED && !opts.force) {
    console.log(`[${SKILL_NAME}] ${label}: ${SKIPPED} (${detail})`);
    return SKIPPED;
  }
  if (!opts.list) {
    if (status === SKIPPED) {
      const backup = await backupDir(label);
      console.log(`[${SKILL_NAME}] ${label}: existing dir moved to ${backup}`);
    }
    await mkdir(path.dirname(label), { recursive: true });
    await cp(SKILL_SRC, label, { recursive: true });
  }
  const verb = opts.list ? "would install" : "installing";
  console.log(`[${SKILL_NAME}] ${label}: ${opts.list ? "PLAN" : INSTALLED} (${verb}: ${detail})`);
  return INSTALLED;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) {
    process.stdout.write(USAGE);
    return 0;
  }
  if (!(await exists(SKILL_SRC))) {
    console.error(`error: skill source not found next to this script: ${SKILL_SRC}`);
    return 1;
  }

  let targets;
  if (opts.dir) {
    targets = [opts.dir];
  } else {
    const scopes = opts.all ? [...USER_SCOPE, ...PROJECT_SCOPE] : USER_SCOPE;
    const found = [];
    for (const d of scopes) if (await exists(d)) found.push(d);
    targets = found;
    if (opts.list) {
      for (const d of scopes) {
        console.log(`target ${d}: ${found.includes(d) ? "detected" : "not present"}`);
      }
    }
  }

  if (targets.length === 0) {
    console.error(
      `no existing skill directory detected (looked in ${USER_SCOPE.join(", ")}); ` +
        `use --dir <path> to choose one.`
    );
    return 1;
  }

  const results = [];
  for (const t of targets) results.push(await installInto(t, opts));

  const installed = results.filter((r) => r === INSTALLED).length;
  const unchanged = results.filter((r) => r === UNCHANGED).length;
  const skipped = results.filter((r) => r === SKIPPED).length;
  console.log(
    `summary: ${installed} installed, ${unchanged} unchanged, ${skipped} skipped ` +
      `(${results.length} target${results.length === 1 ? "" : "s"})`
  );
  return installed + unchanged > 0 ? 0 : 1;
}

process.exit(await main());

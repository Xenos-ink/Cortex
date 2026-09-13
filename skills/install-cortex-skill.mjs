#!/usr/bin/env node
// install-cortex-skill.mjs — the `cortex-skill` bin: side-install/refresh the
// cortex-skill agent skill (fast-path driving for the Cortex computer-use MCP server).
//
// Usage:  cortex-skill install | cortex-skill update   (nothing else)
//
// The skill source (skills/cortex-skill/) is resolved relative to this file, so the
// command works under `npx` straight from the git checkout.
//
// Status lines follow the repo's cortex-mcp CLI conventions:
//   [cortex-skill] <path>: INSTALLED
//   [cortex-skill] <path>: UNCHANGED (identical content)
//   [cortex-skill] <path>: SKIPPED-EXISTS (run `cortex-skill update`)
//   [cortex-skill] <path>: NOT-INSTALLED (run install)
// `install` NEVER overwrites. `update` is the only overwrite path: it backs up each
// existing install to cortex-skill.cortex-backup-<YYYYmmdd-HHMMSS> (an existing backup
// is never overwritten) before replacing it.
//
// Stdlib-only Node >=18 ESM. No telemetry, no network calls.

import { readdir, readFile, mkdir, cp, rename } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const SKILL_NAME = "cortex-skill";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SKILL_SRC = path.join(HERE, SKILL_NAME);

const USER_SCOPE = [".zcode", ".agents", ".claude"].map((d) =>
  path.join(os.homedir(), d, "skills")
);

const INSTALLED = "INSTALLED";
const UNCHANGED = "UNCHANGED";
const SKIPPED = "SKIPPED-EXISTS";
const NOT_INSTALLED = "NOT-INSTALLED";

const USAGE = `cortex-skill — install/update the ${SKILL_NAME} agent skill (fast-path driving for the Cortex computer-use MCP server)

Usage:
  cortex-skill install   install into EVERY detected user-scope skills dir
                         (~/.zcode/skills, ~/.agents/skills, ~/.claude/skills);
                         if none exists, ~/.zcode/skills is created.
                         Never overwrites: a differing existing install is
                         reported SKIPPED-EXISTS (run \`cortex-skill update\`).
  cortex-skill update    refresh only dirs where ${SKILL_NAME} is already installed;
                         backs each up to ${SKILL_NAME}.cortex-backup-<timestamp>
                         before replacing. Dirs without it are reported, not touched.

No other flags. No telemetry, no network.
`;

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

async function dirExists(p) {
  try {
    await readdir(p);
    return true;
  } catch {
    return false;
  }
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
  for (let n = 2; await dirExists(target); n++) {
    target = path.join(parent, `${base}.cortex-backup-${timestamp()}-${n}`);
  }
  await rename(dir, target);
  return target;
}

/** Compare an installed skill dir against the source: missing | identical | differs. */
async function classify(dest) {
  const src = await snapshot(SKILL_SRC);
  let dst;
  try {
    dst = await snapshot(dest);
  } catch {
    return ["missing", "not installed"];
  }
  for (const [rel, buf] of src) {
    const other = dst.get(rel);
    if (other === undefined) return ["differs", `missing file: ${rel}`];
    if (!buf.equals(other)) return ["differs", `differing content: ${rel}`];
  }
  for (const rel of dst.keys()) {
    if (!src.has(rel)) return ["differs", `extra file: ${rel}`];
  }
  return ["identical", "identical content"];
}

async function installInto(dir) {
  const dest = path.join(dir, SKILL_NAME);
  const [kind, detail] = await classify(dest);
  if (kind === "identical") {
    console.log(`[${SKILL_NAME}] ${dest}: ${UNCHANGED} (${detail})`);
    return UNCHANGED;
  }
  if (kind === "differs") {
    console.log(
      `[${SKILL_NAME}] ${dest}: ${SKIPPED} (${detail}; run \`cortex-skill update\`)`
    );
    return SKIPPED;
  }
  await mkdir(dir, { recursive: true });
  await cp(SKILL_SRC, dest, { recursive: true });
  console.log(`[${SKILL_NAME}] ${dest}: ${INSTALLED}`);
  return INSTALLED;
}

async function updateInto(dir) {
  const dest = path.join(dir, SKILL_NAME);
  const [kind, detail] = await classify(dest);
  if (kind === "missing") {
    console.log(`[${SKILL_NAME}] ${dest}: ${NOT_INSTALLED} (run install)`);
    return NOT_INSTALLED;
  }
  if (kind === "identical") {
    console.log(`[${SKILL_NAME}] ${dest}: ${UNCHANGED} (${detail})`);
    return UNCHANGED;
  }
  const backup = await backupDir(dest);
  console.log(`[${SKILL_NAME}] ${dest}: existing dir moved to ${backup}`);
  await cp(SKILL_SRC, dest, { recursive: true });
  console.log(`[${SKILL_NAME}] ${dest}: ${INSTALLED} (refreshed; ${detail})`);
  return INSTALLED;
}

function summary(results, verb) {
  const count = (s) => results.filter((r) => r === s).length;
  console.log(
    `summary: ${count(INSTALLED)} installed, ${count(UNCHANGED)} unchanged, ` +
      `${count(SKIPPED) + count(NOT_INSTALLED)} skipped (${results.length} target${results.length === 1 ? "" : "s"}, ${verb})`
  );
}

async function main() {
  const [cmd] = process.argv.slice(2);
  if (cmd !== "install" && cmd !== "update") {
    process.stderr.write(USAGE);
    return 1;
  }
  if (!(await dirExists(SKILL_SRC))) {
    console.error(`error: skill source not found next to this script: ${SKILL_SRC}`);
    return 1;
  }

  let targets = [];
  for (const d of USER_SCOPE) if (await dirExists(d)) targets.push(d);
  if (targets.length === 0 && cmd === "install") {
    // No user-scope skills dir exists anywhere: bootstrap the primary one.
    targets = [path.join(os.homedir(), ".zcode", "skills")];
  }

  const results = [];
  for (const t of targets) {
    results.push(cmd === "install" ? await installInto(t) : await updateInto(t));
  }
  summary(results, cmd);

  return results.some((r) => r === INSTALLED || r === UNCHANGED) ? 0 : 1;
}

process.exit(await main());

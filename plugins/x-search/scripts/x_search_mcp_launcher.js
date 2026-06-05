#!/usr/bin/env node
"use strict";

const { spawn, spawnSync } = require("child_process");
const path = require("path");

const SERVER_PATH = path.join(__dirname, "x_search_mcp_server.py");
const POSIX_PYTHON_CANDIDATES = [
  { command: "python3", args: [] },
  { command: "python", args: [] },
];
const WINDOWS_PYTHON_CANDIDATES = [
  { command: "py", args: ["-3"] },
  { command: "py", args: [] },
  { command: "python", args: [] },
  { command: "python3", args: [] },
];
const PYTHON_CANDIDATES =
  process.platform === "win32" ? WINDOWS_PYTHON_CANDIDATES : POSIX_PYTHON_CANDIDATES;

function pythonWorks(candidate) {
  const result = spawnSync(
    candidate.command,
    [...candidate.args, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)"],
    { stdio: "ignore", windowsHide: true }
  );
  return result.status === 0;
}

function resolvePython() {
  for (const candidate of PYTHON_CANDIDATES) {
    if (pythonWorks(candidate)) {
      return candidate;
    }
  }
  return null;
}

const python = resolvePython();
if (!python) {
  console.error(
    "X Search could not find Python 3.9 or newer. Install Python from https://www.python.org/downloads/ " +
      "or make python3, python, or the Windows py launcher available on PATH."
  );
  process.exit(127);
}

const child = spawn(
  python.command,
  [...python.args, "-u", SERVER_PATH, ...process.argv.slice(2)],
  {
    cwd: path.resolve(__dirname, ".."),
    stdio: "inherit",
    windowsHide: true,
  }
);

child.on("error", (error) => {
  console.error(`X Search failed to start Python: ${error.message}`);
  process.exit(127);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code === null ? 1 : code);
});

#!/usr/bin/env python3
"""UNFOLD X local worker for OpenCode and GitHub Copilot CLI.

Run this on the machine whose checkout the agent should edit. Authenticate each CLI locally;
UNFOLD X only receives the CLI's stdout over the outbound bridge.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import websockets

BACKEND_WS = os.environ.get("UNFOLDX_BACKEND_WS", "").rstrip("/")
TOKEN = os.environ.get("UNFOLDX_AGENT_BRIDGE_TOKEN", "")
REPO_ROOT = Path(os.environ.get("UNFOLDX_LOCAL_REPO", Path.cwd())).resolve()
PROVIDER = os.environ.get("UNFOLDX_LOCAL_PROVIDER", "").strip().lower()


def resolve(binary: str) -> str:
    configured = os.environ.get(f"UNFOLDX_{binary.upper()}_BIN")
    if configured:
        return configured
    candidates = (f"{binary}.exe", binary, f"{binary}.cmd") if os.name == "nt" else (binary,)
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise FileNotFoundError(
        f"{binary} CLI was not found on PATH. Run 'where {binary}' in PowerShell "
        f"or set UNFOLDX_{binary.upper()}_BIN to its full path."
    )


def build_command(job: dict) -> list[str]:
    prompt = job["prompt"]
    model = job.get("model")
    if PROVIDER == "opencode":
        args = [resolve("opencode"), "run", "--format", "json", "--auto"]
        # 'opencode' was the old UNFOLD X catalog placeholder, not a real model id.
        if model and model != "opencode":
            args += ["--model", model]
        args.append(prompt)
        return args
    if PROVIDER == "github_copilot":
        args = [resolve("copilot"), "-p", prompt, "--allow-all-tools",
                "--allow-all-paths", "--allow-all-urls", "--no-ask-user",
                "--output-format", "json"]
        # 'copilot' was the old catalog placeholder. Otherwise use an actual Copilot model id.
        if model and model != "copilot":
            args += ["--model", model]
        return args
    raise ValueError("UNFOLDX_LOCAL_PROVIDER must be opencode or github_copilot")


async def run_job(ws, job: dict) -> None:
    session_id = job["session_id"]
    if not REPO_ROOT.is_dir():
        await ws.send(json.dumps({
            "type": "error", "session_id": session_id,
            "message": f"Local repo does not exist: {REPO_ROOT}"
        }))
        return
    try:
        args = build_command(job)
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(REPO_ROOT), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            await ws.send(json.dumps({
                "type": "output", "session_id": session_id,
                "line": raw.decode(errors="replace").rstrip("\r\n"),
            }))
        rc = await proc.wait()
        await ws.send(json.dumps({"type": "done", "session_id": session_id, "exit_code": rc}))
    except Exception as exc:
        await ws.send(json.dumps({
            "type": "error", "session_id": session_id,
            "message": f"{type(exc).__name__}: {exc}; provider={PROVIDER}; repo={REPO_ROOT}"
        }))


async def main() -> None:
    if PROVIDER not in {"opencode", "github_copilot"}:
        raise SystemExit("Set UNFOLDX_LOCAL_PROVIDER=opencode or github_copilot.")
    if not BACKEND_WS or not TOKEN:
        raise SystemExit("Set UNFOLDX_BACKEND_WS and UNFOLDX_AGENT_BRIDGE_TOKEN.")
    uri = f"{BACKEND_WS}/ws/agent-bridge?token={TOKEN}"
    print(f"UNFOLD X local worker [{PROVIDER}] -> {BACKEND_WS}")
    print(f"Local repo: {REPO_ROOT}")
    print(f"CLI: {resolve('opencode' if PROVIDER == 'opencode' else 'copilot')}")
    async with websockets.connect(uri, ping_interval=20, ping_timeout=20,
                                  max_size=16 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "hello", "provider": PROVIDER}))
        hello = json.loads(await ws.recv())
        if hello.get("type") != "hello" or hello.get("status") != "ready":
            raise SystemExit(f"Bridge rejected worker: {hello}")
        print("Connected. Waiting for jobs...")
        async for raw in ws:
            job = json.loads(raw)
            if job.get("type") == "job":
                await run_job(ws, job)


if __name__ == "__main__":
    asyncio.run(main())

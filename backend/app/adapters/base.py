"""Common adapter interface (PDF: start(), stream(), status(), entitlement()).

Each provider adapter wraps one CLI in headless mode. The CLI is spawned with an argv list (never a
shell), a scrubbed environment, its own process group (so a stop/circuit-breaker kills children too),
and a hard timeout. If the CLI is not installed and ALLOW_SIMULATION=1, a clearly-labelled simulated
run is used instead so the whole pipeline stays demoable."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from ..catalog import PROVIDERS
from ..config import Settings
from ..models import uid


class AdapterUnavailable(RuntimeError):
    pass


@dataclass
class AgentEvent:
    kind: str                     # log | usage | cost_total | file | result | error
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    files: list[str] = field(default_factory=list)
    stream: str = "stdout"
    metadata: dict = field(default_factory=dict)


@dataclass
class RunRequest:
    prompt: str
    cwd: Path
    mode: str = "execute"         # plan | execute
    model: str | None = None
    env: dict[str, str] = field(default_factory=dict)   # credential env (API key)
    home: Path | None = None
    timeout: float = 900
    session_id: str = field(default_factory=uid)
    title: str = ""
    target_files: list[str] = field(default_factory=list)   # used by the simulator
    # Router decided this provider must NOT run real (it is in a failover bench after a hard
    # failure): run the clearly-labelled simulator instead of the installed-but-doomed CLI.
    force_simulated: bool = False


@dataclass
class RunHandle:
    session_id: str
    provider: str
    simulated: bool
    command_display: str
    proc: asyncio.subprocess.Process | subprocess.Popen | None = None
    started_at: float = field(default_factory=time.monotonic)
    status: str = "running"        # running | exited | killed | timeout | failed
    exit_code: int | None = None
    stop_requested: bool = False
    state: dict = field(default_factory=dict)   # parser scratch state (per run)
    sim_stop: asyncio.Event = field(default_factory=asyncio.Event)


_PASS_ENV = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR", "SYSTEMROOT", "TEMP", "TMP")
_HOST_SESSION_ENV = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "XDG_CONFIG_HOME")


_AUTH_PROBE_SENTINEL = "UAW_AUTH_PROBE_OK"


def _resolve_probe_argv(argv: list[str]) -> list[str]:
    """Resolve a bare executable name into a spawnable argv.

    Windows CreateProcess only appends '.exe' when given a bare name, so npm shims like
    opencode.cmd / codex.cmd are invisible to Popen (WinError 2) even though shutil.which
    finds them — the same rewrite start() does for real runs must apply to probes too."""
    resolved = shutil.which(argv[0])
    if resolved:
        argv = [resolved, *argv[1:]]
    if os.name == "nt" and argv[0].lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", *argv]
    return argv


async def _run_auth_probe(argv: list[str], timeout: float = 20.0) -> bool:
    """Run a tiny non-interactive prompt and return True when the CLI produced a real model
    response (i.e. it is signed in and funded). Shared by every host-session adapter."""
    if _use_threaded_subprocess():
        proc = subprocess.Popen(_resolve_probe_argv(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, env=sandbox_env({}, None, True))
        try:
            out, _ = await asyncio.to_thread(proc.communicate, None, timeout)
        except Exception:
            proc.kill()
            return False
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                *_resolve_probe_argv(argv), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL, env=sandbox_env({}, None, True))
        except (OSError, FileNotFoundError):
            return False
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return False
    return _AUTH_PROBE_SENTINEL in (out or b"").decode(errors="replace")


def _use_threaded_subprocess() -> bool:
    """Use stdlib Popen when Uvicorn's Windows reload loop is a SelectorEventLoop."""
    if os.name != "nt":
        return False
    return isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)


def _split_cmd(raw: str) -> list[str]:
    """Split a command template into argv tokens without mangling Windows paths.

    shlex.split (POSIX) treats '\\' as an escape, so 'C:\\Users\\...' becomes 'C:Users...'. On Windows
    we split on whitespace while honouring single/double quotes as grouping delimiters (e.g. a quoted
    '{prompt}' or a '-c' snippet containing spaces); backslashes stay literal so paths survive."""
    if os.name != "nt":
        return shlex.split(raw)
    toks, cur, quote = [], "", None
    for ch in raw:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if cur:
                toks.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        toks.append(cur)
    return toks


def sandbox_env(extra: dict[str, str], home: Path | None, keep_host_home: bool) -> dict[str, str]:
    env = {k: os.environ[k] for k in _PASS_ENV if k in os.environ}
    if keep_host_home:
        for key in _HOST_SESSION_ENV:
            if key in os.environ:
                env[key] = os.environ[key]
        # Windows CLIs such as Antigravity resolve their config through
        # USERPROFILE/APPDATA even when HOME is present. Derive required
        # profile paths when an IDE/reload process exposes an incomplete env.
        if os.name == "nt":
            profile = env.get("USERPROFILE") or os.environ.get("USERPROFILE") or os.environ.get("HOME")
            if not profile:
                profile = str(Path.home())
            env["USERPROFILE"] = profile
            env.setdefault("HOME", profile)
            env.setdefault("APPDATA", str(Path(profile) / "AppData" / "Roaming"))
            env.setdefault("LOCALAPPDATA", str(Path(profile) / "AppData" / "Local"))
            env.setdefault("HOMEDRIVE", Path(profile).drive)
            env.setdefault("HOMEPATH", str(Path(profile).anchor[len(Path(profile).drive):]) if Path(profile).drive else str(Path(profile)))
    elif home is not None:
        home.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
    env.update(extra)
    return env


def redact(argv: list[str], secrets: list[str]) -> str:
    text = " ".join(shlex.quote(a if len(a) < 120 else a[:117] + "...") for a in argv)
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


class CliAdapter:
    provider: str = ""
    binary: str = ""
    api_key_env_attr: str = ""
    cmd_attr: str = ""
    plan_cmd_attr: str = ""

    def __init__(self, settings: Settings):
        self.settings = settings
        meta = PROVIDERS[self.provider]
        self.display_name = meta["display_name"]
        self.default_model = meta["default_model"]

    # ---- configuration -----------------------------------------------------------------------
    @property
    def api_key_env(self) -> str:
        return getattr(self.settings, self.api_key_env_attr)

    def template(self, mode: str) -> list[str]:
        raw = getattr(self.settings, self.plan_cmd_attr if mode == "plan" else self.cmd_attr)
        return _split_cmd(raw)

    def build_argv(self, req: RunRequest) -> list[str]:
        argv = [tok.replace("{prompt}", req.prompt) for tok in self.template(req.mode)]
        if os.name == "nt":
            # cmd.exe (used to launch npm .cmd shims) treats a newline inside a quoted argument
            # as a command separator, silently truncating multi-line prompts — the CLI then
            # falls back to its interactive greeting. Flatten whitespace so the whole prompt
            # survives as ONE argv token.
            argv = [" ".join(tok.split()) for tok in argv]
        return argv

    def executable(self) -> str:
        return self.template("execute")[0]

    def resolved_executable(self) -> str | None:
        """Absolute path to the launcher, resolving .cmd/.bat/.exe shims (npm global installs on
        Windows ship `codex.cmd`, which create_subprocess_exec cannot spawn by bare name)."""
        return shutil.which(self.executable())

    def available(self) -> bool:
        return self.resolved_executable() is not None

    async def check_auth(self) -> bool:
        """Cheap host-session login probe. Default: NOT verified (False) so callers treat
        host-session providers as unproven instead of assuming a login exists. Api-key-backed
        adapters don't need this (the key itself is the credential)."""
        return False

    # ---- parsing (overridden per provider) ---------------------------------------------------------
    def parse_json_event(self, obj: dict, state: dict) -> list[AgentEvent]:
        from .normalize import generic_event
        return generic_event(obj, state)

    def parse_line(self, line: str, state: dict) -> list[AgentEvent]:
        s = line.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return self.parse_json_event(obj, state)
            except json.JSONDecodeError:
                pass
        state["last_text"] = s
        return [AgentEvent("log", text=s)]

    # ---- lifecycle -----------------------------------------------------------------------------------
    async def version(self) -> str | None:
        if not self.available():
            return None
        try:
            if _use_threaded_subprocess():
                proc = subprocess.Popen(
                    _resolve_probe_argv([self.executable(), "--version"]), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=sandbox_env({}, None, True))
                out, _ = await asyncio.to_thread(proc.communicate, timeout=10)
            else:
                proc = await asyncio.create_subprocess_exec(
                    *_resolve_probe_argv([self.executable(), "--version"]), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(proc.communicate(), 10)
            return out.decode(errors="replace").strip().splitlines()[0][:120] if out else None
        except Exception:
            return None

    async def start(self, req: RunRequest, *, keep_host_home: bool = False) -> RunHandle:
        argv = self.build_argv(req)
        # Antigravity is a host-session CLI: its language server/config
        # discovery requires the real Windows user profile.
        if os.name == "nt" and self.provider == "gemini":
            keep_host_home = True
        secrets = list(req.env.values())
        if not self.available() or req.force_simulated:
            if not self.settings.allow_simulation:
                raise AdapterUnavailable(
                    f"{self.display_name}: executable '{self.executable()}' not found on PATH and simulation is disabled")
            return RunHandle(req.session_id, self.provider, True, "[simulated] " + redact(argv, secrets))
        req.cwd.mkdir(parents=True, exist_ok=True)
        env = sandbox_env(req.env, req.home, keep_host_home)
        resolved = self.resolved_executable()
        if resolved and os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
            # npm shims are batch files: only cmd.exe can execute them. argv[0] is replaced by
            # the shim path and invoked through the shell interpreter with the original args.
            argv = [env.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", resolved, *argv[1:]]
        elif resolved:
            argv[0] = resolved  # pin the exact binary found on PATH (no re-resolution races)
        if _use_threaded_subprocess():
            proc = subprocess.Popen(
                argv, cwd=str(req.cwd), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=str(req.cwd), env=env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True, limit=8 * 1024 * 1024)
            except (OSError, FileNotFoundError) as e:
                # create_subprocess_exec on Windows cannot launch cmd.exe batch shims directly.
                # Fall back to cmd.exe /c with the ORIGINAL argv if the shim rewrite didn't help.
                if os.name != "nt" or argv[0].lower().endswith((".cmd", ".bat")):
                    raise
                batch = shutil.which(argv[0])
                if batch is None or not batch.lower().endswith((".cmd", ".bat")):
                    raise
                cmd_argv = [env.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", batch, *argv[1:]]
                proc = await asyncio.create_subprocess_exec(
                    *cmd_argv, cwd=str(req.cwd), env=env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True, limit=8 * 1024 * 1024)
        return RunHandle(req.session_id, self.provider, False, redact(argv, secrets), proc=proc)

    async def stream(self, handle: RunHandle, req: RunRequest) -> AsyncIterator[AgentEvent]:
        if handle.simulated:
            from .simulated import simulate
            async for ev in simulate(self, handle, req):
                yield ev
            return
        proc = handle.proc
        assert proc is not None and proc.stdout is not None
        threaded = isinstance(proc, subprocess.Popen)
        deadline = handle.started_at + req.timeout
        read_task = asyncio.create_task(asyncio.to_thread(proc.stdout.readline)) if threaded else None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                handle.status = "timeout"
                await self.stop(handle)
                if read_task:
                    await read_task
                yield AgentEvent("error", text=f"{self.display_name} timed out after {int(req.timeout)}s")
                return
            try:
                if threaded:
                    assert read_task is not None
                    raw = await asyncio.wait_for(asyncio.shield(read_task), min(remaining, 5))
                    read_task = None
                else:
                    raw = await asyncio.wait_for(proc.stdout.readline(), min(remaining, 5))
            except asyncio.TimeoutError:
                continue
            except (ValueError, asyncio.LimitOverrunError):
                yield AgentEvent("log", text="[output line exceeded size limit and was dropped]")
                continue
            if not raw:
                break
            text = raw.decode(errors="replace").rstrip("\r\n")
            if text.strip():
                for ev in self.parse_line(text, handle.state):
                    yield ev
            if threaded:
                read_task = asyncio.create_task(asyncio.to_thread(proc.stdout.readline))
        rc = await asyncio.to_thread(proc.wait) if threaded else await proc.wait()
        handle.exit_code = rc
        if handle.stop_requested:
            handle.status = "killed"
        elif rc != 0:
            handle.status = "failed"
            yield AgentEvent("error", text=f"{self.display_name} exited with code {rc}")
        else:
            handle.status = "exited"

    async def status(self, handle: RunHandle) -> dict:
        if handle.simulated:
            alive = handle.status == "running" and not handle.sim_stop.is_set()
        else:
            alive = handle.proc is not None and handle.proc.returncode is None
        return {"session_id": handle.session_id, "provider": self.provider, "simulated": handle.simulated,
                "status": handle.status, "alive": bool(alive), "exit_code": handle.exit_code,
                "elapsed_s": round(time.monotonic() - handle.started_at, 2)}

    async def stop(self, handle: RunHandle) -> None:
        handle.stop_requested = True
        handle.sim_stop.set()
        proc = handle.proc
        if proc is None or proc.returncode is not None:
            return
        if isinstance(proc, subprocess.Popen):
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    await asyncio.to_thread(proc.wait, 3)
                except subprocess.TimeoutExpired:
                    pass
        elif os.name == "nt":
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    pass
        if handle.status == "running":
            handle.status = "killed"

    async def entitlement(self, conn, ledger: dict) -> dict:
        """Plan/quota view. No cross-vendor introspection protocol exists, so this is derived from the
        local metering ledger plus what the CLI reports (version/availability)."""
        pricing = conn.pricing if conn else {}
        quota = None
        if pricing.get("model") == "seat" and pricing.get("monthly_request_quota"):
            q = int(pricing["monthly_request_quota"])
            quota = {"monthly_request_quota": q, "requests_used": ledger["requests"],
                     "requests_remaining": max(0, q - ledger["requests"])}
        return {"provider": self.provider, "display_name": self.display_name, "cli_available": self.available(),
                "cli_version": await self.version(), "mode": "real" if self.available() else (
                    "simulated" if self.settings.allow_simulation else "unavailable"),
                "plan": conn.plan if conn else None, "pricing_model": pricing.get("model"), "quota": quota,
                "source": "local-metering (no vendor introspection API)"}

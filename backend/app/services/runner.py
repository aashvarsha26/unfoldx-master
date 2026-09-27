"""Runs one agent process and turns its output into canonical events + budget accounting.

Pipeline per line: adapter.parse_line -> AgentEvent -> log_line / budget_update / circuit_breaker ...
appended to the hash-chained log (which fans out over Redis/WebSocket)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..adapters.base import AdapterUnavailable, RunHandle, RunRequest
from ..adapters.normalize import is_cli_auth_error, is_cli_permission_error
from ..catalog import estimate_cost
from ..models import Agent

log = logging.getLogger("uaw.runner")
MAX_LINE = 2000
MAX_BUFFER = 200_000


def _looks_like_auth_error(text: str) -> bool:
    return is_cli_auth_error(text or "")


@dataclass
class RunOutcome:
    ok: bool = False
    final_text: str = ""
    files: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    tokens: int = 0
    tripped: bool = False
    stopped: bool = False
    error: str | None = None
    error_kind: str | None = None   # None | "auth" | "permission" | "crash"
    session_id: str = ""
    simulated: bool = False


class AgentRunner:
    def __init__(self, ctx):
        self.ctx = ctx

    async def _run_local_agent(self, *, ws_id: str, task_id: str | None, subtask_id: str | None,
                              agent: Agent, req: RunRequest, conn, ident: dict,
                              on_file: Callable[[str], Awaitable[None]] | None) -> RunOutcome:
        ctx = self.ctx
        adapter = ctx.adapters[agent.provider]
        out = RunOutcome(session_id=req.session_id, simulated=False)
        await ctx.budget.record(ws_id, agent.provider, requests=1)
        await ctx.events.append(ws_id, "dispatch_started", {
            "command": f"[local worker] {agent.provider} CLI", "agent": agent.name,
            "provider": agent.provider, "mode": req.mode, "simulated": False, "title": req.title,
            "execution": "local_worker"}, **ident)
        try:
            queue = await ctx.local_agents.enqueue(provider=agent.provider, session_id=req.session_id, workspace_id=ws_id,
                                                  prompt=req.prompt, mode=req.mode, model=req.model)
        except RuntimeError as e:
            out.error = str(e)
            out.error_kind = "auth"
            return out
        buffer, result_text = [], ""
        result_metadata: dict = {}
        state = {}
        try:
            while True:
                msg = await asyncio.wait_for(queue.get(), timeout=req.timeout)
                typ = msg.get("type")
                if typ == "output":
                    line = str(msg.get("line", ""))
                    if len("".join(buffer)) < MAX_BUFFER:
                        buffer.append(line)
                    for ev in adapter.parse_line(line, state):
                        if ev.kind == "log":
                            await ctx.events.append(ws_id, "log_line", {"line": ev.text[:MAX_LINE], "stream": "stdout"}, **ident)
                        elif ev.kind == "result":
                            result_text = ev.text or result_text
                            if ev.metadata:
                                result_metadata = dict(ev.metadata)
                        elif ev.kind == "file":
                            for f in ev.files:
                                if f not in out.files:
                                    out.files.append(f)
                                if on_file:
                                    await on_file(f)
                        elif ev.kind == "usage":
                            delta = estimate_cost(conn.pricing, ev.tokens_in, ev.tokens_out)
                            out.cost_usd += delta
                            out.tokens += ev.tokens_in + ev.tokens_out
                            await ctx.budget.record(ws_id, agent.provider, cost=delta,
                                                    tokens_in=ev.tokens_in, tokens_out=ev.tokens_out)
                elif typ == "done":
                    rc = msg.get("exit_code", 1)
                    if rc == 0:
                        out.ok = True
                    else:
                        out.error = f"{adapter.display_name} local worker exited with code {rc}"
                        out.error_kind = "crash"
                    break
                elif typ == "error":
                    out.error = str(msg.get("message", "local Codex worker failed"))
                    out.error_kind = "crash"
                    break
        except asyncio.TimeoutError:
            out.error = f"{adapter.display_name} local worker timed out after {int(req.timeout)}s"
            out.error_kind = "crash"
        finally:
            ctx.local_agents.finish(req.session_id)
        out.final_text = result_text or "\n".join(buffer[-20:])
        if out.ok and req.mode != "plan" and out.final_text.strip():
            await ctx.events.append(ws_id, "agent_output", {
                "text": out.final_text.strip()[:20_000], "simulated": False,
                "structured": bool(result_metadata), "result": result_metadata or None,
                "files": out.files, "execution": "local_worker"}, **ident)
        return out

    async def run(self, *, ws_id: str, task_id: str | None, subtask_id: str | None, agent: Agent, req: RunRequest,
                  on_handle: Callable[[RunHandle], None] | None = None,
                  on_file: Callable[[str], Awaitable[None]] | None = None) -> RunOutcome:
        ctx = self.ctx
        adapter = ctx.adapters[agent.provider]
        out = RunOutcome(session_id=req.session_id)
        conn = await ctx.entitlement.get_conn(ws_id, agent.provider)
        if conn is None:
            out.error = f"{adapter.display_name} is not connected to this workspace"
            return out
        env, keep_home = ctx.entitlement.credential_env(conn, adapter)
        req.env, req.home = env, ctx.settings.workspaces_root / ws_id / "home" / agent.provider
        ident = dict(agent_id=agent.id, task_id=task_id, subtask_id=subtask_id, provider=agent.provider,
                     model=req.model or agent.model, session_id=req.session_id)
        # Codex can run on the user's machine using ChatGPT OAuth instead of an API key.
        # Railway remains the orchestrator; the local worker owns the authenticated CLI process.
        if agent.provider in {"codex", "opencode", "github_copilot"} and ctx.local_agents.connected(agent.provider):
            return await self._run_local_agent(ws_id=ws_id, task_id=task_id, subtask_id=subtask_id,
                                               agent=agent, req=req, conn=conn, ident=ident, on_file=on_file)

        try:
            handle = await adapter.start(req, keep_host_home=keep_home)
        except AdapterUnavailable as e:
            out.error = str(e)
            return out
        except OSError as e:
            out.error = f"could not start {adapter.display_name}: {e}"
            return out
        out.simulated = handle.simulated
        if on_handle:
            on_handle(handle)
        await ctx.budget.record(ws_id, agent.provider, requests=1)
        await ctx.events.append(ws_id, "dispatch_started", {
            "command": handle.command_display, "agent": agent.name, "provider": agent.provider, "mode": req.mode,
            "simulated": handle.simulated, "title": req.title}, **ident)

        counted = 0.0
        buffer: list[str] = []
        buf_len = 0
        result_text = ""
        result_metadata: dict = {}
        error_seen: str | None = None
        hard_cli_failure = False
        auth_error_text = ""
        permission_error_text = ""
        gen = adapter.stream(handle, req)
        try:
            async for ev in gen:
                if ev.kind == "log":
                    txt = ev.text[:MAX_LINE]
                    if buf_len < MAX_BUFFER:
                        buffer.append(ev.text)
                        buf_len += len(ev.text)
                    payload = {"line": txt, "stream": ev.stream}
                    if handle.simulated:
                        payload["simulated"] = True
                    await ctx.events.append(ws_id, "log_line", payload, **ident)
                elif ev.kind in ("usage", "cost_total"):
                    if ev.kind == "usage":
                        delta = estimate_cost(conn.pricing, ev.tokens_in, ev.tokens_out)
                        tin, tout = ev.tokens_in, ev.tokens_out
                    else:  # vendor-reported total: reconcile our running estimate to it
                        delta, tin, tout = (ev.cost_usd or 0.0) - counted, 0, 0
                    counted += delta
                    out.cost_usd, out.tokens = counted, out.tokens + tin + tout
                    st = await ctx.budget.record(ws_id, agent.provider, cost=delta, tokens_in=tin, tokens_out=tout)
                    await ctx.events.append(ws_id, "budget_update", {
                        "provider": agent.provider, "spent_usd": st["spent_usd"], "cap_usd": st["cap_usd"],
                        "remaining_usd": st["remaining_usd"], "tokens_in": st["tokens_in"], "tokens_out": st["tokens_out"],
                        "breaker": st["breaker_state"]}, cost_delta=delta, tokens_delta=tin + tout, **ident)
                    if st["tripped_now"]:
                        await ctx.events.append(ws_id, "circuit_breaker_triggered", {
                            "provider": agent.provider, "spent_usd": st["spent_usd"], "cap_usd": st["cap_usd"],
                            "detail": f"Stop-loss: {adapter.display_name} reached its ${st['cap_usd']:.2f} cap "
                                      f"(${st['spent_usd']:.2f} spent). Process stopped; work paused until an approver overrides."},
                            **ident)
                    if st["breaker_state"] == "open":
                        out.tripped = True
                        await adapter.stop(handle)
                        break
                elif ev.kind == "file":
                    for f in ev.files:
                        if f not in out.files:
                            out.files.append(f)
                        if on_file:
                            await on_file(f)
                elif ev.kind == "result":
                    result_text = ev.text or result_text
                    if ev.metadata:
                        result_metadata = dict(ev.metadata)
                elif ev.kind == "error":
                    if is_cli_permission_error(ev.text):
                        # Signed-in but the headless permission policy refused the tool call.
                        # Distinct status so the UI can say "fix the CLI's permission mode",
                        # not "log in". Still a hard failure: the run cannot continue.
                        hard_cli_failure = True
                        permission_error_text = ev.text[:MAX_LINE]
                        await ctx.events.append(ws_id, "log_line", {
                            "line": permission_error_text, "stream": "stderr", "level": "warning",
                            "provider_status": "cli_permission_denied"}, **ident)
                    elif is_cli_auth_error(ev.text):
                        # The CLI itself is not signed in / not funded: demote to a warning log
                        # line (keeps the event log honest without red-failing every subtask)
                        # and mark the run as a hard CLI failure so the orchestrator benches the
                        # provider and re-routes the subtask instead of failing the task.
                        hard_cli_failure = True
                        auth_error_text = ev.text[:MAX_LINE]
                        await ctx.events.append(ws_id, "log_line", {
                            "line": auth_error_text, "stream": "stderr", "level": "warning",
                            "provider_status": "cli_not_signed_in"}, **ident)
                    elif hard_cli_failure and "exited with code" in ev.text:
                        # The non-zero exit that follows an auth failure is the same root cause;
                        # keep the log clean and let failover tell the story once.
                        await ctx.events.append(ws_id, "log_line", {
                            "line": ev.text[:MAX_LINE], "stream": "stderr", "level": "warning",
                            "provider_status": "cli_not_signed_in"}, **ident)
                    else:
                        error_seen = ev.text
                        await ctx.events.append(ws_id, "error", {"message": ev.text[:MAX_LINE], "provider": agent.provider}, **ident)
        finally:
            await gen.aclose()
            if handle.proc is not None and handle.proc.returncode is None:
                await adapter.stop(handle)

        out.stopped = handle.stop_requested and not out.tripped
        out.final_text = result_text or "\n".join(buffer[-20:])
        if error_seen:
            out.error = error_seen
            # Some CLIs write their login banner to stderr only (merged into the buffer) and
            # exit non-zero without emitting any JSON error event — classify from the buffer
            # so failover treats it as an auth failure and benches the provider.
            if _looks_like_auth_error("\n".join(buffer)):
                out.error_kind = "auth"
        elif hard_cli_failure and permission_error_text:
            out.error = f"{adapter.display_name} denied a tool call (headless permission policy): {permission_error_text}"
            out.error_kind = "permission"
        elif hard_cli_failure:
            out.error = f"{adapter.display_name} is not signed in: {auth_error_text}"
            out.error_kind = "auth"
        out.ok = not (out.tripped or out.stopped or out.error) and (handle.simulated or handle.exit_code in (0, None))
        if not out.ok and not out.error and not (out.tripped or out.stopped):
            out.error = f"{adapter.display_name} exited with code {handle.exit_code}"
            out.error_kind = "crash"

        # Stream the agent's raw response to the UI as one agent_output event
        # (the Output panel groups these per task). Only successful runs emit:
        # partial transcript lines already flowed as log_line events.
        if out.ok and req.mode != "plan" and out.final_text.strip():
            await ctx.events.append(ws_id, "agent_output", {
                "text": out.final_text.strip()[:20_000],
                "simulated": handle.simulated,
                "structured": bool(result_metadata),
                "result": result_metadata or None,
                "files": out.files,
            }, **ident)
        return out

"""Orchestration: plan -> route -> conflict-check -> headless dispatch -> handoff.

Planning uses Bob Plan-mode whenever a connected, ENABLED Bob is available. When Bob is not connected
(enabled) the plan comes from the project's heuristic planner instead -- a Bob connection is never
faked. If Bob is unavailable AND simulation is disabled, the task fails with a clear error rather than
pretending Bob exists. Routing decisions start from each plan's per-subtask capability tags and file
claims; agents whose CLI is not installed are excluded before scoring whenever a real CLI exists, so the
best genuinely-available executor (e.g. Antigravity/Gemini) is chosen instead of a simulated run."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from ..adapters.base import RunHandle, RunRequest
from ..models import (Agent, Attachment, ConflictRecord, HandoffObject, Route, Subtask, Task, uid)
from .budget import BudgetService
from .conflicts import colliding_paths
from .planning import (HANDOFF_INSTRUCTIONS, Plan, build_plan_prompt, heuristic_plan, parse_handoff, parse_plan)
from .routing import Candidate, RouteFailure, choose

log = logging.getLogger("uaw.orchestrator")
TERMINAL = {"completed", "failed", "cancelled"}
AUTH_FAILURE_HINTS = ("not signed in", "not logged in", "please run /login", "unauthorized", "invalid api key",
                      "authentication", "api key", "credit balance")
AUTH_BENCH_SECONDS = 600.0  # a signed-out CLI cannot recover mid-task; bench it for the whole task
# Attachments (design templates, PRDs, specs) are copied verbatim into prompts so agents build
# to the actual spec instead of a filename. Bounded so a huge upload cannot blow the context.
ATTACH_TEXT_SUFFIXES = (".md", ".txt", ".html", ".htm", ".css", ".js", ".json", ".csv", ".svg", ".xml",
                        ".yml", ".yaml", ".py", ".ts", ".tsx", ".rst")
ATTACH_MAX_CHARS_PER_FILE = 6000
ATTACH_MAX_TOTAL_CHARS = 24000


def _looks_like_auth_failure(msg: str) -> bool:
    low = (msg or "").lower()
    return any(h in low for h in AUTH_FAILURE_HINTS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RunningSub:
    subtask_id: str
    task_id: str
    agent_id: str
    provider: str
    files: list[str]
    handle: RunHandle | None = None
    redirected: bool = False
    aio: asyncio.Task | None = None


@dataclass
class TaskRuntime:
    task_id: str
    ws_id: str
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: bool = False
    finished: bool = False
    plan_handle: RunHandle | None = None
    main: asyncio.Task | None = None
    seen_runtime_conflicts: set = field(default_factory=set)


def _attachment_texts(attachments: list) -> list[tuple[str, str]]:
    """(filename, bounded text content) for text-like attachments; binaries are named only."""
    out: list[tuple[str, str]] = []
    budget = ATTACH_MAX_TOTAL_CHARS
    for a in attachments:
        name = getattr(a, "filename", "file")
        path = getattr(a, "path", "")
        text = ""
        if path and name.lower().endswith(ATTACH_TEXT_SUFFIXES):
            try:
                raw = Path(path).read_text(encoding="utf-8", errors="replace")[:max(0, budget)]
                text = raw[:ATTACH_MAX_CHARS_PER_FILE]
                budget -= len(text)
            except OSError:
                text = ""
        if budget <= 0 and not text:
            continue
        out.append((name, text))
        if budget <= 0:
            break
    return out


class Orchestrator:
    def __init__(self, ctx):
        self.ctx = ctx
        self.running: dict[str, dict[str, RunningSub]] = {}
        self.tasks: dict[str, TaskRuntime] = {}
        self._dispatch_locks: dict[str, asyncio.Lock] = {}
        # provider -> wall-clock deadline while a real CLI is benched after a hard failure
        # (bad login, crash, non-zero exit). Keeps one broken CLI from poisoning a whole task:
        # the router moves the work to the next-best agent and the provider gets a fresh
        # chance on later tasks. Empty/simulated failures never bench anybody.
        self.provider_cooldowns: dict[str, float] = {}
        # workspace -> coordination state for spreading parallel subtasks across providers:
        # the provider of the most recent dispatch and a round-robin counter of dispatches.
        self._last_dispatched_provider: dict[str, str | None] = {}
        self._rr_counter: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers
    @property
    def sm(self):
        return self.ctx.db.sessionmaker

    def repo_dir(self, ws_id: str):
        d = self.ctx.settings.workspaces_root / ws_id / "repo"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _running(self, ws: str) -> dict[str, RunningSub]:
        return self.running.setdefault(ws, {})

    def _wake_workspace(self, ws: str) -> None:
        for rt in self.tasks.values():
            if rt.ws_id == ws:
                rt.wake.set()

    async def _set(self, sub_id: str, **vals) -> None:
        async with self.sm() as s:
            await s.execute(update(Subtask).where(Subtask.id == sub_id).values(**vals))
            await s.commit()

    async def _candidates(self, ws: str) -> list[Candidate]:
        async with self.sm() as s:
            agents = list((await s.execute(select(Agent).where(Agent.workspace_id == ws))).scalars())
        conns = {c.provider: c for c in await self.ctx.entitlement.list_conns(ws)}
        budgets = {b["provider"]: b for b in await self.ctx.budget.all(ws)}
        now = _now().timestamp()
        out = []
        for a in agents:
            c, b = conns.get(a.provider), budgets.get(a.provider)
            if c is None or b is None:
                continue
            # A provider bench-cooldown counts as temporarily unavailable for real runs; the
            # simulated fallback stays usable so the pipeline keeps flowing. Credential truth
            # (API key in DB/env, verified CLI login) is surfaced in provider_status(); a
            # dispatch that still hits a signed-out CLI fails fast, benches it, and the
            # router re-routes — no per-dispatch login probe (too slow/costly here).
            cli_available = self.ctx.adapters[a.provider].available() and now >= self.provider_cooldowns.get(a.provider, 0.0)
            benched = a.provider in self.provider_cooldowns and now < self.provider_cooldowns[a.provider]
            quota = None
            if c.pricing.get("model") == "seat" and c.pricing.get("monthly_request_quota"):
                quota = int(c.pricing["monthly_request_quota"]) - b["requests"]
            out.append(Candidate(a.id, a.provider, a.name, a.capabilities or {}, c.pricing, b, a.enabled, quota,
                                 cli_available=cli_available, benched=benched))
        return out

    def provider_failed(self, provider: str) -> None:
        """Bench a provider whose real CLI just failed hard (auth error, crash, timeout).
        Also benches providers whose CLI is not installed so re-routing during a task does
        not repeatedly attempt doomed simulated runs when a real provider is available."""
        if self.ctx.settings.failover_cooldown_seconds > 0:
            self.provider_cooldowns[provider] = _now().timestamp() + self.ctx.settings.failover_cooldown_seconds

    def provider_auth_failed(self, provider: str) -> None:
        """Bench a provider whose CLI is signed out (or key is missing). Unlike a crash, this
        cannot recover mid-task, so bench for AUTH_BENCH_SECONDS regardless of the (shorter)
        failover cooldown setting — sibling subtasks must skip the dead CLI instead of each
        paying a doomed attempt."""
        self.provider_cooldowns[provider] = _now().timestamp() + AUTH_BENCH_SECONDS

    # ------------------------------------------------------------------ submit
    async def submit_task(self, ws_id: str, user_id: str | None, prompt: str, attachment_ids: list[str]) -> Task:
        async with self.sm() as s:
            atts = []
            if attachment_ids:
                atts = list((await s.execute(select(Attachment).where(
                    Attachment.workspace_id == ws_id, Attachment.id.in_(attachment_ids)))).scalars())
                missing = set(attachment_ids) - {a.id for a in atts}
                if missing:
                    raise ValueError(f"unknown attachment ids: {sorted(missing)}")
            task = Task(workspace_id=ws_id, prompt=prompt, created_by=user_id, attachment_ids=attachment_ids)
            s.add(task)
            await s.commit()
        await self.ctx.events.append(ws_id, "task_submitted", {
            "summary": prompt[:300], "task_id": task.id, "submitted_by": user_id,
            "attachments": [a.filename for a in atts]}, task_id=task.id)
        rt = TaskRuntime(task.id, ws_id)
        self.tasks[task.id] = rt
        rt.main = asyncio.create_task(self._run_task(rt), name=f"task-{task.id}")
        return task

    async def _run_task(self, rt: TaskRuntime) -> None:
        try:
            planned = await self._plan(rt)
            if planned is None or rt.stopped:
                return
            plan, mode, planner = planned
            await self._create_subtasks(rt, plan, mode, planner)
            await self._schedule(rt)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let a bug leave a task hanging silently
            log.exception("task %s crashed", rt.task_id)
            await self._finish(rt, "failed", f"internal orchestrator error: {e}")

    # ------------------------------------------------------------------ plan (Bob, or heuristic fallback)
    async def _plan(self, rt: TaskRuntime) -> tuple[Plan, str, str | None] | None:
        ws = rt.ws_id
        async with self.sm() as s:
            task = await s.get(Task, rt.task_id)
            bob = (await s.execute(select(Agent).where(Agent.workspace_id == ws, Agent.provider == "bob",
                                                       Agent.enabled.is_(True)).limit(1))).scalar_one_or_none()
            atts = list((await s.execute(select(Attachment).where(Attachment.id.in_(task.attachment_ids or [])))).scalars())
        cands = await self._candidates(ws)
        # Bob is "available" only when actually connected AND enabled (candidates require conn + budget).
        bob_connected = bob is not None and any(c.provider == "bob" and c.enabled for c in cands)
        if bob_connected:
            prompt = build_plan_prompt(task.prompt, [a.filename for a in atts],
                                       [{"provider": c.provider, "capabilities": c.capabilities} for c in cands if c.enabled],
                                       attachment_texts=_attachment_texts(atts))
            req = RunRequest(prompt=prompt, cwd=self.repo_dir(ws), mode="plan", model=bob.model,
                             timeout=self.ctx.settings.plan_timeout_seconds, title="Bob Plan-mode decomposition")

            def keep(h): rt.plan_handle = h
            outcome = await self.ctx.runner.run(ws_id=ws, task_id=task.id, subtask_id=None, agent=bob, req=req, on_handle=keep)
            await self._add_cost(None, task.id, outcome)
            if rt.stopped:
                return None
            if outcome.tripped:
                await self._finish(rt, "failed", "Bob's budget cap was reached during planning; raise the cap and resubmit")
                return None
            plan = parse_plan(outcome.final_text) if outcome.ok else None
            if plan is not None:
                return plan, ("simulated" if outcome.simulated else "bob"), "bob"
            if outcome.error and not outcome.simulated:
                self.provider_failed("bob")  # hard CLI failure (bad auth/crash): bench it and fall back
            reason = outcome.error or "Bob's plan output was not valid plan JSON"
            await self.ctx.events.append(ws, "log_line", {
                "line": f"Bob plan unusable ({reason}); falling back to heuristic decomposition", "level": "warning"},
                task_id=task.id, provider="bob", agent_id=bob.id)
            fallback = parse_plan("```json\n" + json.dumps(heuristic_plan(task.prompt)) + "\n```")
            return fallback, "heuristic-fallback", "bob"  # type: ignore[return-value]
        if rt.stopped:
            return None
        if not self.ctx.settings.allow_simulation:
            await self._finish(rt, "failed",
                               "No planner available: Bob is not connected to this workspace and simulation is disabled")
            return None
        await self.ctx.events.append(ws, "log_line", {
            "line": "Bob is not connected; using heuristic decomposition instead of Bob Plan-mode", "level": "warning"},
            task_id=task.id)
        fallback = parse_plan("```json\n" + json.dumps(heuristic_plan(task.prompt)) + "\n```")
        return fallback, "heuristic-fallback", None  # type: ignore[return-value]

    async def _create_subtasks(self, rt: TaskRuntime, plan: Plan, mode: str, planner: str | None = "bob") -> None:
        ids = [uid() for _ in plan.subtasks]
        async with self.sm() as s:
            for i, st in enumerate(plan.subtasks):
                s.add(Subtask(id=ids[i], task_id=rt.task_id, workspace_id=rt.ws_id, idx=i, title=st.title,
                              description=st.description or st.title, capabilities=st.capabilities,
                              target_files=st.files, depends_on=[ids[d] for d in st.depends_on]))
            await s.execute(update(Task).where(Task.id == rt.task_id).values(status="running"))
            await s.commit()
        await self.ctx.events.append(rt.ws_id, "plan_decomposed", {
            "subtasks": [st.title for st in plan.subtasks], "rationale": plan.rationale,
            "planner": {"provider": planner, "mode": mode},
            "plan": [{"id": ids[i], "title": st.title, "capabilities": st.capabilities, "files": st.files,
                      "depends_on": [ids[d] for d in st.depends_on]} for i, st in enumerate(plan.subtasks)]},
            task_id=rt.task_id, provider=planner)

    # ------------------------------------------------------------------ schedule
    async def _load_subs(self, task_id: str) -> list[Subtask]:
        async with self.sm() as s:
            return list((await s.execute(select(Subtask).where(Subtask.task_id == task_id).order_by(Subtask.idx))).scalars())

    async def _schedule(self, rt: TaskRuntime) -> None:
        while True:
            rt.wake.clear()
            if rt.stopped or rt.finished:
                return
            subs = await self._load_subs(rt.task_id)
            by_id = {s.id: s for s in subs}
            for sub in subs:
                if sub.status in ("pending", "blocked") and any(
                        by_id[d].status in ("failed", "cancelled") for d in sub.depends_on if d in by_id):
                    await self._set(sub.id, status="cancelled", error="a dependency failed or was cancelled")
                    sub.status = "cancelled"
                    await self.ctx.events.append(rt.ws_id, "log_line", {
                        "line": f"Subtask '{sub.title}' cancelled: a dependency did not complete"}, task_id=rt.task_id, subtask_id=sub.id)
            for sub in subs:
                if sub.status in ("pending", "blocked") and all(
                        by_id[d].status == "completed" for d in sub.depends_on if d in by_id):
                    await self._try_dispatch(rt, sub)
            subs = await self._load_subs(rt.task_id)
            if all(s.status in TERMINAL for s in subs):
                break
            try:
                await asyncio.wait_for(rt.wake.wait(), 10)
            except asyncio.TimeoutError:
                pass
        failed = [s for s in subs if s.status == "failed"]
        if rt.stopped or rt.finished:
            return
        if failed or any(s.status == "cancelled" for s in subs):
            await self._finish(rt, "failed", "; ".join(f"'{s.title}': {s.error}" for s in subs if s.status in ("failed", "cancelled")))
        else:
            await self._finish(rt, "completed", " | ".join(s.result_summary or s.title for s in subs)[:1500])

    async def _try_dispatch(self, rt: TaskRuntime, sub: Subtask) -> None:
        ws = rt.ws_id
        async with self._dispatch_locks.setdefault(ws, asyncio.Lock()):
            running = self._running(ws)
            if sub.id in running or len(running) >= self.ctx.settings.max_concurrent_subtasks or rt.stopped:
                return
            cands = await self._candidates(ws)
            decision, announce = None, True
            try:
                if sub.status == "blocked" and sub.agent_id and not sub.forced_agent_id:
                    try:  # already routed and only waiting on a conflict: keep the same agent if still viable
                        decision = choose(sub.capabilities, sub.description, cands, forced_agent_id=sub.agent_id)
                        announce = False
                    except RouteFailure:
                        decision = None
                if decision is None:
                    ws_state = self.running.get(ws, {})
                    # Coordination: while siblings are in flight, rotate near-equal candidates across
                    # distinct providers (avoiding whoever just took the previous dispatch) so parallel
                    # work spreads by strength instead of piling onto one AI. Only when more than one
                    # viable provider is connected, and only for unforced picks.
                    avoid, rr = None, 0
                    if ws_state:
                        viable_providers = {c.provider for c in cands if c.enabled and c.budget["remaining_usd"] > 0}
                        if len(viable_providers) > 1:
                            avoid = self._last_dispatched_provider.get(ws)
                            rr = self._rr_counter.get(ws, 0)
                    decision = choose(sub.capabilities, sub.description, cands, forced_agent_id=sub.forced_agent_id,
                                      avoid_provider=avoid, rr_counter=rr)
                    self._rr_counter[ws] = rr + 1
            except RouteFailure as f:
                if f.budget_blocked:
                    await self._set(sub.id, status="paused_budget")
                    await self.ctx.events.append(ws, "log_line", {
                        "line": f"Subtask '{sub.title}' paused: {f.reason}. Waiting for a budget override."},
                        task_id=rt.task_id, subtask_id=sub.id)
                else:
                    await self._set(sub.id, status="failed", error=f.reason, finished_at=_now())
                    await self.ctx.events.append(ws, "error", {"message": f"Cannot route '{sub.title}': {f.reason}"},
                                                 task_id=rt.task_id, subtask_id=sub.id)
                return
            if announce:
                async with self.sm() as s:
                    s.add(Route(workspace_id=ws, subtask_id=sub.id, agent_id=decision.agent_id, provider=decision.provider,
                                score=decision.score, est_cost_usd=decision.est_cost_usd, quota_remaining_usd=decision.remaining_usd,
                                rationale=decision.rationale, breakdown=decision.breakdown))
                    await s.commit()
                await self.ctx.events.append(ws, "route_decided", {
                    "agent": decision.provider, "reason": decision.rationale, "subtask": sub.title, "score": decision.score,
                    "est_cost_usd": decision.est_cost_usd, "remaining_usd": decision.remaining_usd,
                    "breakdown": decision.breakdown}, task_id=rt.task_id, subtask_id=sub.id,
                    agent_id=decision.agent_id, provider=decision.provider)
            if announce:
                self._last_dispatched_provider[ws] = decision.provider
            # ---- conflict check against every other in-flight subtask in the workspace
            blockers = [(o, colliding_paths(sub.target_files, o.files)) for o in running.values()]
            blockers = [(o, p) for o, p in blockers if p]
            # Provenance for the clean path too: the UI shows every dispatch was
            # conflict-checked, not only the ones that found a collision.
            await self.ctx.events.append(ws, "conflict_checked", {
                "subtask": sub.title, "against": [o.subtask_id for o in running.values()],
                "checked_paths": sorted({p for _, paths in blockers for p in paths} or list(sub.target_files)),
                "overlaps": [p for _, paths in blockers for p in paths], "result": "deferred" if blockers else "clear"},
                task_id=rt.task_id, subtask_id=sub.id, agent_id=decision.agent_id, provider=decision.provider)
            if blockers:
                if sub.status != "blocked":
                    async with self.sm() as s:
                        for o, paths in blockers:
                            s.add(ConflictRecord(workspace_id=ws, subtask_id=sub.id, other_subtask_id=o.subtask_id, paths=paths))
                        await s.commit()
                    await self.ctx.events.append(ws, "conflict_detected", {
                        "detail": f"'{sub.title}' overlaps files claimed by a running subtask; deferring until it finishes",
                        "paths": sorted({x for _, p in blockers for x in p}), "blocked_by": [o.subtask_id for o, _ in blockers],
                        "averted": True, "resolution": "deferred"}, task_id=rt.task_id, subtask_id=sub.id,
                        agent_id=decision.agent_id, provider=decision.provider)
                await self._set(sub.id, status="blocked", agent_id=decision.agent_id, provider=decision.provider, forced_agent_id=None)
                return
            async with self.sm() as s:  # any earlier deferral is now resolved
                await s.execute(update(ConflictRecord).where(ConflictRecord.subtask_id == sub.id, ConflictRecord.resolved_at.is_(None))
                                .values(resolved_at=_now()))
                await s.commit()
            rs = RunningSub(sub.id, rt.task_id, decision.agent_id, decision.provider, list(sub.target_files))
            running[sub.id] = rs  # claim files BEFORE any further await
            await self._set(sub.id, status="running", agent_id=decision.agent_id, provider=decision.provider,
                            forced_agent_id=None, attempts=sub.attempts + 1, started_at=_now(), session_id=uid(), error=None)
            rs.aio = asyncio.create_task(self._execute(rt, rs), name=f"sub-{sub.id}")

    # ------------------------------------------------------------------ execute one subtask
    async def _build_prompt(self, task: Task, sub: Subtask) -> str:
        async with self.sm() as s:
            hands = list((await s.execute(select(HandoffObject).where(HandoffObject.subtask_id.in_(sub.depends_on or [])))).scalars())
            atts = list((await s.execute(select(Attachment).where(Attachment.id.in_(task.attachment_ids or [])))).scalars())
        parts = [f"You are one agent in a multi-agent development workspace. Overall goal:\n{task.prompt}\n",
                 f"YOUR SUBTASK: {sub.title}\n{sub.description}\n"]
        if sub.target_files:
            parts.append("Only modify these paths (other agents work on other paths at the same time): " + ", ".join(sub.target_files))
        for name, text in _attachment_texts(atts):
            if text:
                parts.append(f"Attached file '{name}' (content to follow and honour exactly — it is the design/spec for this work):\n"
                             f"--- BEGIN {name} ---\n{text}\n--- END {name} ---")
            else:
                parts.append(f"Attached file (not readable as text, ask before assuming its contents): {name}")
        for h in hands:
            parts.append("Handoff from a completed dependency:\n" + json.dumps({
                "summary": h.summary, "decisions": h.decisions, "constraints": h.constraints,
                "rejected_approaches": h.rejected_approaches, "files_touched": h.files_touched}, indent=1))
        parts.append(HANDOFF_INSTRUCTIONS)
        return "\n\n".join(parts)

    async def _execute(self, rt: TaskRuntime, rs: RunningSub) -> None:
        ws, sub_id = rt.ws_id, rs.subtask_id
        try:
            async with self.sm() as s:
                sub, task, agent = await s.get(Subtask, sub_id), await s.get(Task, rt.task_id), await s.get(Agent, rs.agent_id)
            if agent is None:
                await self._set(sub_id, status="failed", error="agent no longer exists", finished_at=_now())
                return
            now_ts = _now().timestamp()
            was_benched = agent.provider in self.provider_cooldowns and now_ts < self.provider_cooldowns[agent.provider]
            req = RunRequest(prompt=await self._build_prompt(task, sub), cwd=self.repo_dir(ws), mode="execute", model=agent.model,
                             timeout=self.ctx.settings.subtask_timeout_seconds, session_id=sub.session_id or uid(),
                             title=sub.title, target_files=list(sub.target_files),
                             force_simulated=was_benched)  # benched CLI: simulated run only, never another real attempt

            def keep(h): rs.handle = h
            async def on_file(path: str): await self._runtime_conflict(rt, rs, path)
            outcome = await self.ctx.runner.run(ws_id=ws, task_id=rt.task_id, subtask_id=sub_id, agent=agent, req=req,
                                                on_handle=keep, on_file=on_file)
            await self._add_cost(sub_id, rt.task_id, outcome)
            ident = dict(task_id=rt.task_id, subtask_id=sub_id, agent_id=agent.id, provider=agent.provider,
                         model=agent.model, session_id=req.session_id)
            if rt.stopped:
                return  # stop_task already recorded cancellation
            if rs.redirected:
                await self._set(sub_id, status="pending", error=None)
                await self.ctx.events.append(ws, "log_line", {"line": f"Subtask '{sub.title}' pulled from {agent.name} for redirect"}, **ident)
            elif outcome.tripped:
                await self._set(sub_id, status="paused_budget", error="budget cap reached")
                await self.ctx.events.append(ws, "log_line", {
                    "line": f"Subtask '{sub.title}' paused: {agent.name} hit its budget cap. Approve an override or redirect to another agent."}, **ident)
            elif outcome.ok:
                await self._complete(ws, rt, sub, agent, req, outcome, ident)
            else:
                msg = outcome.error or "agent failed"
                # Failover: a hard failure of a REAL CLI (bad login, crash, timeout) benches the
                # provider and re-queues the subtask, so the router picks the next-best agent
                # (or a simulated run) instead of failing the whole task. The attempt cap
                # (default 4, one per provider) bounds cost: if every real executor is down the
                # final failure is reported as-is.
                if not outcome.simulated and not rs.redirected and sub.attempts < self.ctx.settings.max_subtask_attempts:
                    if outcome.error_kind == "auth" or _looks_like_auth_failure(msg):
                        self.provider_auth_failed(agent.provider)
                    elif outcome.error_kind == "permission":
                        # Signed-in but the headless policy denies tool calls: re-running on the
                        # same provider is hopeless until the CLI's permission mode is fixed.
                        # Bench like an auth failure so the router moves on immediately.
                        self.provider_auth_failed(agent.provider)
                    else:
                        self.provider_failed(agent.provider)
                    await self._set(sub_id, status="pending", agent_id=agent.id,
                                    forced_agent_id=None, error=None, finished_at=None)
                    await self.ctx.events.append(ws, "log_line", {
                        "line": f"Subtask '{sub.title}' failed on {agent.name} ({msg}); re-routing to the next best agent",
                        "level": "warning", "failover": True}, **ident)
                else:
                    await self._set(sub_id, status="failed", error=msg, finished_at=_now())
                    await self.ctx.events.append(ws, "error", {"message": f"Subtask '{sub.title}' failed: {msg}"}, **ident)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("subtask %s crashed", sub_id)
            await self._set(sub_id, status="failed", error=f"internal error: {e}", finished_at=_now())
            await self.ctx.events.append(ws, "error", {"message": f"Subtask crashed: {e}"}, task_id=rt.task_id, subtask_id=sub_id)
        finally:
            self._running(ws).pop(sub_id, None)
            self._wake_workspace(ws)

    async def _complete(self, ws, rt, sub, agent, req, outcome, ident) -> None:
        parsed = parse_handoff(outcome.final_text)
        files = list(dict.fromkeys((parsed["files_touched"] if parsed else []) + outcome.files))
        summary = (parsed or {}).get("summary") or (outcome.final_text.strip()[:500] if outcome.final_text else sub.title)
        h = HandoffObject(workspace_id=ws, task_id=rt.task_id, subtask_id=sub.id, agent_id=agent.id, provider=agent.provider,
                          model=agent.model, session_id=req.session_id, decisions=(parsed or {}).get("decisions", []),
                          constraints=(parsed or {}).get("constraints", []),
                          rejected_approaches=(parsed or {}).get("rejected_approaches", []), files_touched=files,
                          summary=summary, structured=parsed is not None)
        async with self.sm() as s:
            s.add(h)
            await s.commit()
        await self._set(sub.id, status="completed", result_summary=summary[:1000], finished_at=_now())
        # Raw agent output for this subtask, emitted alongside (and never instead of) the
        # structured handoff below: final_text is the CLI's `result` text when it produced one,
        # otherwise the runner's captured tail of the log buffer.
        await self.ctx.events.append(ws, "agent_output", {
            "subtask": sub.title, "text": outcome.final_text[:8000], "truncated": len(outcome.final_text) > 8000,
            "simulated": outcome.simulated, "structured": parsed is not None}, **ident)
        await self.ctx.events.append(ws, "handoff_emitted", {
            "handoff_id": h.id, "subtask": sub.title, "summary": summary, "decisions": h.decisions,
            "constraints": h.constraints, "rejected_approaches": h.rejected_approaches, "files_touched": files,
            "structured": h.structured}, **ident)

    async def _runtime_conflict(self, rt: TaskRuntime, rs: RunningSub, path: str) -> None:
        """A running agent edited a file another in-flight subtask has claimed: detected, not averted."""
        for o in list(self._running(rt.ws_id).values()):
            if o.subtask_id == rs.subtask_id or not colliding_paths([path], o.files):
                continue
            key = (rs.subtask_id, o.subtask_id, path)
            if key in rt.seen_runtime_conflicts:
                continue
            rt.seen_runtime_conflicts.add(key)
            async with self.sm() as s:
                s.add(ConflictRecord(workspace_id=rt.ws_id, subtask_id=rs.subtask_id, other_subtask_id=o.subtask_id,
                                     paths=[path], resolution="detected_runtime"))
                await s.commit()
            await self.ctx.events.append(rt.ws_id, "conflict_detected", {
                "detail": f"Runtime collision: {path} was edited outside its claimed paths and is claimed by another running subtask",
                "paths": [path], "blocked_by": [o.subtask_id], "averted": False, "resolution": "detected_runtime"},
                task_id=rt.task_id, subtask_id=rs.subtask_id, agent_id=rs.agent_id, provider=rs.provider)

    async def _add_cost(self, sub_id: str | None, task_id: str, outcome) -> None:
        async with self.sm() as s:
            if sub_id:
                sub = await s.get(Subtask, sub_id)
                sub.cost_usd += outcome.cost_usd
                sub.tokens += outcome.tokens
            t = await s.get(Task, task_id)
            t.cost_usd += outcome.cost_usd
            t.tokens += outcome.tokens
            await s.commit()

    async def _finish(self, rt: TaskRuntime, status: str, summary: str) -> None:
        if rt.finished:
            return
        rt.finished = True
        async with self.sm() as s:
            task = await s.get(Task, rt.task_id)
            task.status, task.summary, task.completed_at = status, summary, _now()
            cost, tokens = task.cost_usd, task.tokens
            await s.commit()
        if status == "failed":
            await self.ctx.events.append(rt.ws_id, "error", {"message": f"Task failed: {summary}", "task_id": rt.task_id,
                                                              "status": "failed"}, task_id=rt.task_id)
        else:  # completed | stopped share task_completed; `status` distinguishes them
            await self.ctx.events.append(rt.ws_id, "task_completed", {
                "summary": summary, "status": status, "task_id": rt.task_id, "cost_usd": round(cost, 6), "tokens": tokens},
                task_id=rt.task_id, cost_delta=0.0)
        self.tasks.pop(rt.task_id, None)

    # ------------------------------------------------------------------ control actions
    async def stop_task(self, task_id: str, by: str) -> bool:
        rt = self.tasks.get(task_id)
        if rt is None or rt.finished:
            return False
        rt.stopped = True
        if rt.plan_handle:
            await self.ctx.adapters["bob"].stop(rt.plan_handle)
        mine = [r for r in self._running(rt.ws_id).values() if r.task_id == task_id]
        for r in mine:
            if r.handle:
                await self.ctx.adapters[r.provider].stop(r.handle)
        async with self.sm() as s:
            await s.execute(update(Subtask).where(Subtask.task_id == task_id, Subtask.status.notin_(list(TERMINAL)))
                            .values(status="cancelled", error=f"stopped by {by}", finished_at=_now()))
            await s.commit()
        await asyncio.gather(*[r.aio for r in mine if r.aio], return_exceptions=True)
        if rt.main and rt.main is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(rt.main), 5)
            except (asyncio.TimeoutError, Exception):
                pass
        await self.ctx.events.append(rt.ws_id, "log_line", {"line": f"Task stopped by {by}"}, task_id=task_id)
        await self._finish(rt, "stopped", f"Stopped by {by}")
        self._wake_workspace(rt.ws_id)
        return True

    async def redirect_subtask(self, task_id: str, sub_id: str, by: str, agent_id: str | None, instruction: str | None) -> str:
        rt = self.tasks.get(task_id)
        async with self.sm() as s:
            sub = await s.get(Subtask, sub_id)
            if sub is None or sub.task_id != task_id:
                raise LookupError("subtask not found")
            if rt is None or sub.status in TERMINAL:
                raise ValueError(f"subtask is {sub.status}; only active subtasks can be redirected")
            if agent_id:
                ag = await s.get(Agent, agent_id)
                if ag is None or ag.workspace_id != sub.workspace_id:
                    raise LookupError("agent not found in this workspace")
            desc = sub.description + (f"\n\nUser redirect instruction: {instruction}" if instruction else "")
            sub.description, sub.forced_agent_id = desc, agent_id
            await s.commit()
        rs = self._running(rt.ws_id).get(sub_id)
        await self.ctx.events.append(rt.ws_id, "log_line", {
            "line": f"{by} redirected '{sub.title}'" + (f" to agent {agent_id}" if agent_id else "") +
                    (f": {instruction}" if instruction else "")}, task_id=task_id, subtask_id=sub_id)
        if rs:
            rs.redirected = True
            if rs.handle:
                await self.ctx.adapters[rs.provider].stop(rs.handle)
        else:
            # Not in flight (paused on budget, or waiting between scheduler passes): re-queue it so the
            # forced agent takes over on the next pass. The status guard makes the re-queue atomic —
            # a subtask whose run finished between the status check above and this update is left alone
            # instead of being resurrected and re-run.
            async with self.sm() as s:
                res = await s.execute(update(Subtask).where(Subtask.id == sub_id, Subtask.status.notin_(list(TERMINAL)))
                                      .values(status="pending"))
                await s.commit()
            if res.rowcount:
                rt.wake.set()
        return "redirected"

    async def resume_paused(self, ws_id: str) -> int:
        """After a budget override/cap raise: put paused subtasks back in the queue."""
        async with self.sm() as s:
            rows = list((await s.execute(select(Subtask).where(Subtask.workspace_id == ws_id, Subtask.status == "paused_budget"))).scalars())
            for r in rows:
                r.status, r.error = "pending", None
            await s.commit()
        self._wake_workspace(ws_id)
        return len(rows)

    async def recover_interrupted(self) -> None:
        """Processes cannot survive an API restart: mark in-flight work failed instead of leaving it 'running'."""
        async with self.sm() as s:
            tasks = list((await s.execute(select(Task).where(Task.status.in_(["planning", "running"])))).scalars())
            for t in tasks:
                t.status, t.summary, t.completed_at = "failed", "interrupted by server restart", _now()
                await s.execute(update(Subtask).where(Subtask.task_id == t.id, Subtask.status.notin_(list(TERMINAL)))
                                .values(status="failed", error="interrupted by server restart"))
            await s.commit()
        for t in tasks:
            await self.ctx.events.append(t.workspace_id, "error", {"message": "Task interrupted by server restart", "task_id": t.id}, task_id=t.id)

    async def shutdown(self) -> None:
        for rt in list(self.tasks.values()):
            rt.stopped = True
        for ws in list(self.running.values()):
            for r in list(ws.values()):
                if r.handle:
                    await self.ctx.adapters[r.provider].stop(r.handle)
        tasks = [rt.main for rt in self.tasks.values() if rt.main]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

"""Outbound local-agent bridge for provider CLIs authenticated on the user's machine.

Railway never receives local CLI credentials. Local workers open outbound WebSockets,
identify the provider they can execute, receive only that provider's jobs, and stream
stdout back to the orchestrator.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class _Job:
    provider: str
    session_id: str
    workspace_id: str
    prompt: str
    mode: str
    model: str | None
    queue: asyncio.Queue


class LocalAgentBridge:
    def __init__(self, token: str | None):
        self.token = token or ""
        self._pending: dict[str, asyncio.Queue[_Job]] = {}
        self._jobs: dict[str, _Job] = {}
        self._worker_counts: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def connected(self, provider: str) -> bool:
        return self._worker_counts.get(provider, 0) > 0

    async def enqueue(self, *, provider: str, session_id: str, workspace_id: str,
                      prompt: str, mode: str, model: str | None = None) -> asyncio.Queue:
        if not self.enabled:
            raise RuntimeError("agent bridge is not configured")
        q = asyncio.Queue(maxsize=1000)
        job = _Job(provider, session_id, workspace_id, prompt, mode, model, q)
        self._jobs[session_id] = job
        self._pending.setdefault(provider, asyncio.Queue())
        await self._pending[provider].put(job)
        return q

    async def cancel(self, session_id: str) -> None:
        job = self._jobs.get(session_id)
        if job:
            await job.queue.put({"type": "cancel"})

    def finish(self, session_id: str) -> None:
        self._jobs.pop(session_id, None)

    async def worker_loop(self, websocket) -> None:
        hello = await websocket.receive_json()
        provider = str(hello.get("provider", "")).strip()
        if not provider:
            await websocket.close(code=1008)
            return

        queue = self._pending.setdefault(provider, asyncio.Queue())
        self._worker_counts[provider] = self._worker_counts.get(provider, 0) + 1
        await websocket.send_json({"type": "hello", "provider": provider, "status": "ready"})
        try:
            while True:
                job = await queue.get()
                await websocket.send_json({
                    "type": "job",
                    "provider": job.provider,
                    "session_id": job.session_id,
                    "workspace_id": job.workspace_id,
                    "prompt": job.prompt,
                    "mode": job.mode,
                    "model": job.model,
                })
                while True:
                    msg = await websocket.receive_json()
                    if msg.get("session_id") != job.session_id:
                        continue
                    await job.queue.put(msg)
                    if msg.get("type") in {"done", "error"}:
                        self.finish(job.session_id)
                        break
        finally:
            self._worker_counts[provider] = max(0, self._worker_counts.get(provider, 1) - 1)
            for job in list(self._jobs.values()):
                if job.provider == provider:
                    await job.queue.put({
                        "type": "error",
                        "message": f"local {provider} worker disconnected"
                    })
                    self.finish(job.session_id)

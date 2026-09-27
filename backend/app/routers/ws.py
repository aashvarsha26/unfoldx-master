"""WebSocket feed: GET /ws/workspace/{workspace_id} -> one WorkspaceEvent JSON object per frame.

On connect the last N events (default 200, `?replay=N`) or everything after `?since_seq=K` is replayed,
then live events follow; sequence numbers de-duplicate the replay/live seam. Auth: `?token=` (JWT) - in
AUTH_MODE=open anonymous viewers are allowed. Requires at least the 'view' role."""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi import status as st

from ..bus import OVERFLOW
from ..deps import RANK, resolve_user, role_for
from ..models import Workspace

router = APIRouter()
log = logging.getLogger("uaw.ws")


@router.websocket("/ws/workspace/{workspace_id}")
async def feed(websocket: WebSocket, workspace_id: str):
    ctx = websocket.app.state.ctx
    q = websocket.query_params
    user = None
    role = None
    async with ctx.db.sessionmaker() as s:
        try:
            user = await resolve_user(ctx, s, q.get("token"))
        except Exception:
            pass
        if user is not None:
            ws = await s.get(Workspace, workspace_id)
            role = await role_for(ctx, s, workspace_id, user) if ws else None
    if role is None or RANK[role] < RANK["view"]:
        await websocket.close(code=st.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        async with ctx.bus.subscribe(workspace_id) as queue:  # subscribe BEFORE reading the backlog: no gap
            since = q.get("since_seq")
            if since and since.isdigit():
                backlog = await ctx.events.list(workspace_id, int(since), 5000)
            else:
                n = int(q.get("replay", ctx.settings.ws_replay_default)) if q.get("replay", "1").isdigit() else 200
                backlog = await ctx.events.tail(workspace_id, min(n, 5000)) if n else []
            last = 0
            for e in backlog:
                await websocket.send_json(e)
                last = e["seq"]

            async def pump():
                nonlocal last
                while True:
                    e = await queue.get()
                    if e is OVERFLOW:
                        await websocket.close(code=st.WS_1013_TRY_AGAIN_LATER)
                        return
                    if e["seq"] > last:
                        last = e["seq"]
                        await websocket.send_json(e)

            async def drain():  # client -> server frames are ignored (control actions use REST + RBAC); this detects disconnects
                while True:
                    await websocket.receive_text()

            tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                exc = t.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                    log.warning("ws closed: %r", exc)
    except WebSocketDisconnect:
        pass


@router.websocket("/ws/agent-bridge")
async def local_worker(websocket: WebSocket):
    """Authenticated outbound worker channel for local provider agents."""
    ctx = websocket.app.state.ctx
    token = websocket.query_params.get("token", "")
    if not ctx.local_agents.enabled or token != ctx.local_agents.token:
        await websocket.close(code=st.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    try:
        await ctx.local_agents.worker_loop(websocket)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("local worker connection failed")

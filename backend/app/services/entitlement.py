"""Per-provider auth, plan/quota visibility. Credentials are Fernet-encrypted at rest and only
decrypted in memory to build the child-process environment. Official API keys / host CLI logins only:
no session scraping."""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..adapters.base import CliAdapter
from ..catalog import PROVIDERS, normalize_pricing
from ..config import Settings
from ..events import EventService
from ..models import Agent, ProviderConnection
from ..security import Vault
from .budget import BudgetService

log = logging.getLogger("uaw.entitlement")


class EntitlementService:
    def __init__(self, sm: async_sessionmaker, settings: Settings, vault: Vault, adapters: dict[str, CliAdapter],
                 budget: BudgetService, events: EventService):
        self._sm, self._settings, self._vault = sm, settings, vault
        self._adapters, self._budget, self._events = adapters, budget, events
        self._version_cache: dict[str, tuple[float, str | None]] = {}
        self._local_agent_broker = None
        self._poller: asyncio.Task | None = None

    # ---- connect / disconnect ---------------------------------------------------------------------
    async def connect(self, ws_id: str, provider: str, *, user_id: str | None, api_key: str | None = None,
                      plan: str = "unknown", pricing: dict | None = None, cap_usd: float | None = None) -> dict:
        if provider not in PROVIDERS:
            raise ValueError(f"unsupported provider {provider!r}; supported: {', '.join(PROVIDERS)}")
        adapter = self._adapters[provider]
        pr = normalize_pricing(provider, pricing)
        auth_type = "api_key" if api_key else ("host_session" if adapter.available() or (self._local_agent_broker and self._local_agent_broker.connected(provider)) else "none")
        cipher = self._vault.encrypt(api_key) if api_key else None
        meta = PROVIDERS[provider]
        async with self._sm() as s:
            conn = (await s.execute(select(ProviderConnection).where(
                ProviderConnection.workspace_id == ws_id, ProviderConnection.provider == provider))).scalar_one_or_none()
            if conn is None:
                conn = ProviderConnection(workspace_id=ws_id, provider=provider)
                s.add(conn)
            conn.auth_type, conn.plan, conn.pricing, conn.connected_by = auth_type, plan, pr, user_id
            if api_key:
                conn.secret_ciphertext = cipher
            elif auth_type != "api_key":
                conn.secret_ciphertext = None
            has_agent = (await s.execute(select(Agent.id).where(
                Agent.workspace_id == ws_id, Agent.provider == provider).limit(1))).first()
            if not has_agent:
                s.add(Agent(workspace_id=ws_id, provider=provider, name=meta["display_name"],
                            model=meta["default_model"], capabilities=dict(meta["capabilities"])))
            await s.commit()
        ledger = await self._budget.ensure(ws_id, provider, cap_usd if cap_usd is not None else self._settings.default_budget_cap_usd)
        if cap_usd is not None and abs(ledger["cap_usd"] - cap_usd) > 1e-9:
            ledger = await self._budget.set_cap(ws_id, provider, cap_usd)
        # Legacy seed names from older catalog revisions ("Codex CLI", "IBM Bob", "Claude Code",
        # "Bob Shell", "Gemini CLI") used to stick in the DB forever and leak into routing
        # rationales and failover lines. Heal them to the current catalog display name.
        if has_agent:
            async with self._sm() as s:
                for row in (await s.execute(select(Agent).where(
                        Agent.workspace_id == ws_id, Agent.provider == provider))).scalars():
                    if row.name != meta["display_name"]:
                        row.name = meta["display_name"]
                await s.commit()
        mode = "real" if adapter.available() else ("simulated" if self._settings.allow_simulation else "unavailable")
        note = {"real": "CLI found on host", "simulated": f"'{adapter.executable()}' CLI not installed - running SIMULATED agent",
                "unavailable": f"'{adapter.executable()}' CLI not installed and simulation disabled"}[mode]
        await self._events.append(
            ws_id, "provider_connected",
            {"provider": provider, "detail": f"{adapter.display_name} connected ({auth_type}); {note}", "mode": mode,
             "auth_type": auth_type, "plan": plan, "pricing_model": pr["model"], "cap_usd": ledger["cap_usd"]},
            provider=provider)
        return await self.provider_status(ws_id, provider)

    async def disconnect(self, ws_id: str, provider: str) -> bool:
        async with self._sm() as s:
            res = await s.execute(delete(ProviderConnection).where(
                ProviderConnection.workspace_id == ws_id, ProviderConnection.provider == provider))
            await s.execute(delete(Agent).where(Agent.workspace_id == ws_id, Agent.provider == provider))
            await s.commit()
            return res.rowcount > 0

    # ---- reads -------------------------------------------------------------------------------------
    async def get_conn(self, ws_id: str, provider: str) -> ProviderConnection | None:
        async with self._sm() as s:
            return (await s.execute(select(ProviderConnection).where(
                ProviderConnection.workspace_id == ws_id, ProviderConnection.provider == provider))).scalar_one_or_none()

    async def list_conns(self, ws_id: str) -> list[ProviderConnection]:
        async with self._sm() as s:
            return list((await s.execute(select(ProviderConnection).where(
                ProviderConnection.workspace_id == ws_id).order_by(ProviderConnection.provider))).scalars())

    async def _version(self, provider: str) -> str | None:
        ts, v = self._version_cache.get(provider, (0.0, None))
        if time.monotonic() - ts > max(30.0, self._settings.entitlement_poll_seconds):
            v = await self._adapters[provider].version()
            self._version_cache[provider] = (time.monotonic(), v)
        return v

    async def provider_status(self, ws_id: str, provider: str) -> dict:
        conn = await self.get_conn(ws_id, provider)
        ledger = await self._budget.state(ws_id, provider) or {"requests": 0}
        ent = await self._adapters[provider].entitlement(conn, ledger)
        ent["cli_version"] = await self._version(provider) if ent["cli_available"] else None
        # Resolve the real credential state so the UI can distinguish between three distinct
        # failure modes (CLI missing / credentials missing / local worker offline) rather
        # than collapsing them all into a generic "not connected" label.
        adapter = self._adapters[provider]
        cred = self.credential_env(conn, adapter) if conn else ({}, False)
        env_key_present = any(v for v in cred[0].values())
        # API-key adapters (Bob, Gemini, Codex, OpenCode): check_auth() tests key presence
        # cheaply from os.environ — no network probe. Host-session adapters run a live probe
        # only when the CLI is actually installed (Railway: not installed → skip probe).
        host_login = False
        if conn and conn.auth_type == "host_session":
            host_login = await adapter.check_auth()
        if conn is None:
            auth_state = "disconnected"
        elif cred[0]:
            # A usable API key was resolved (from DB or env).
            auth_state = "api_key"
        elif conn.auth_type == "local_worker":
            # Local-agent bridge: worker present = authenticated, absent = offline.
            if self._local_agent_broker and self._local_agent_broker.connected(provider):
                auth_state = "local_worker"
            else:
                auth_state = "worker_offline"
        elif conn.auth_type == "host_session":
            if not ent["cli_available"]:
                # CLI binary not installed on this host (e.g. Railway without agy).
                auth_state = "cli_missing"
            elif host_login:
                auth_state = "host_session"
            else:
                auth_state = "signed_out"
        else:
            # Connected but no API key in DB/env and not a session/worker type.
            if not ent["cli_available"]:
                auth_state = "cli_missing"
            else:
                auth_state = "auth_missing"
        ent.update(connected=conn is not None, auth_type=conn.auth_type if conn else None,
                   auth_state=auth_state,
                   authenticated=auth_state in ("api_key", "host_session", "local_worker"),
                   credential_stored=bool(conn and conn.secret_ciphertext),  # never the secret itself
                   env_key_present=env_key_present,
                   credential_hint=self._credential_hint(provider, auth_state),
                   budget=ledger if conn else None)
        return ent

    def _credential_hint(self, provider: str, auth_state: str) -> str | None:
        adapter = self._adapters[provider]
        env_name = adapter.api_key_env
        return {
            "disconnected": f"Connect {adapter.display_name} (POST /providers with api_key, or this panel).",
            "cli_missing": f"{adapter.display_name} CLI ('{adapter.executable()}') is not installed on this host. "
                           f"Set {env_name} to use the API-key path, or connect a local worker.",
            "auth_missing": f"No credentials for {adapter.display_name}: set the {env_name} env var or store an API key here.",
            "signed_out": f"{adapter.display_name} CLI is installed but not signed in. "
                          f"Run '{adapter.executable()} login' on the host, or store an API key (env var: {env_name}).",
            "no_credentials": f"No credentials: store an API key (env var name: {env_name}) or sign in on the host.",
            "worker_offline": f"{adapter.display_name} is configured for the local-agent bridge but no worker is connected. "
                              f"Start the local agent on your machine (UNFOLDX_AGENT_BRIDGE_TOKEN must match).",
        }.get(auth_state)

    async def status(self, ws_id: str) -> list[dict]:
        return [await self.provider_status(ws_id, c.provider) for c in await self.list_conns(ws_id)]

    def credential_env(self, conn: ProviderConnection, adapter: CliAdapter) -> tuple[dict[str, str], bool]:
        """(env for the child process, keep host HOME so a host CLI login is usable).

        Resolution order: the workspace-stored encrypted key, then a usable value of the
        provider's env var in the backend process environment (shell or backend/.env —
        config._finalize imports those values). A host-session connection keeps HOME so the
        host CLI login applies."""
        if conn.auth_type == "api_key" and conn.secret_ciphertext:
            return {adapter.api_key_env: self._vault.decrypt(conn.secret_ciphertext)}, False
        # Fall back to the backend process env (shell export or .env value). This is what
        # makes "I put BOBSHELL_API_KEY=... in .env" actually work.
        import os
        val = os.environ.get(adapter.api_key_env)
        if val:
            return {adapter.api_key_env: val}, False
        # IBM Bob Shell's current non-interactive docs use BOB_API_KEY. Older UNFOLD X
        # deployments used BOBSHELL_API_KEY, so keep the legacy name as a compatibility
        # fallback rather than silently breaking an existing Railway secret.
        if adapter.provider == "bob":
            legacy = os.environ.get("BOBSHELL_API_KEY")
            if legacy:
                return {"BOB_API_KEY": legacy}, False
        return {}, conn.auth_type == "host_session"

    async def _probe_auth(self, provider: str) -> bool:
        # kept for compatibility with older callers/tests
        return await self._adapters[provider].check_auth()

    # ---- polling -----------------------------------------------------------------------------------
    def start_polling(self) -> None:
        if self._poller is None and self._settings.entitlement_poll_seconds > 0:
            self._poller = asyncio.create_task(self._poll(), name="entitlement-poller")

    async def stop_polling(self) -> None:
        if self._poller:
            self._poller.cancel()
            try:
                await self._poller
            except (asyncio.CancelledError, Exception):
                pass
            self._poller = None

    async def _poll(self) -> None:
        while True:
            try:
                for p in self._adapters:  # refresh CLI availability/version so status reads stay cheap
                    self._version_cache[p] = (time.monotonic(), await self._adapters[p].version())
            except Exception:
                log.exception("entitlement poll failed")
            await asyncio.sleep(self._settings.entitlement_poll_seconds)

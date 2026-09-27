from __future__ import annotations

from sqlalchemy import select

from .adapters.providers import build_adapters
from .bus import InMemoryBus, RedisBus
from .config import Settings
from .db import Database
from .events import EventService
from .local_worker import LocalAgentBridge
from .security import Vault
from .services.budget import BudgetService
from .services.entitlement import EntitlementService
from .services.orchestrator import Orchestrator
from .services.runner import AgentRunner

# Agent names previous catalog revisions seeded into the DB ("Codex CLI", "IBM Bob", ...).
# They leak into routing rationales and failover lines, so they are always safe to overwrite
# with the provider's canonical display name at startup. A user-customized name (anything
# else, set via PATCH /agents) is never touched.
_LEGACY_AGENT_NAMES = {"Codex CLI", "IBM Bob", "Bob Shell", "Claude Code", "Claude CLI",
                       "Gemini CLI", "Google Antigravity CLI", "GitHub Copilot CLI"}


class AppContext:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.database_url)
        self.bus = RedisBus(settings.redis_url) if settings.redis_url else InMemoryBus()
        self.vault = Vault(settings.fernet_key, settings.secret_key)
        self.events = EventService(self.db.sessionmaker, self.bus, settings.secret_key)
        self.local_agents = LocalAgentBridge(settings.agent_bridge_token)
        self.adapters = build_adapters(settings)
        self.budget = BudgetService(self.db.sessionmaker)
        self.entitlement = EntitlementService(self.db.sessionmaker, settings, self.vault, self.adapters, self.budget, self.events)
        self.entitlement._local_agent_broker = self.local_agents
        self.runner = AgentRunner(self)
        self.orchestrator = Orchestrator(self)

    async def startup(self) -> None:
        await self.db.create_all()
        await self._heal_agent_names()
        await self._purge_removed_providers()
        await self.bus.start()
        await self.orchestrator.recover_interrupted()
        if self.settings.seed_demo_workspace and self.settings.auth_mode == "open":
            from .services.workspaces import seed_demo
            await seed_demo(self)
        self.entitlement.start_polling()

    async def _purge_removed_providers(self) -> None:
        """Providers removed from the catalog (e.g. claude_code) must not leave connections,
        agents or budget rows behind: provider_status() would KeyError on the missing adapter."""
        from sqlalchemy import delete
        from .models import Agent, BudgetLedger, ProviderConnection
        async with self.db.sessionmaker() as s:
            rows = list((await s.execute(select(ProviderConnection.provider))).all())
            stale = [r[0] for r in rows if r[0] not in self.adapters]
            for p in stale:
                await s.execute(delete(Agent).where(Agent.provider == p))
                await s.execute(delete(BudgetLedger).where(BudgetLedger.provider == p))
                await s.execute(delete(ProviderConnection).where(ProviderConnection.provider == p))
            if stale:
                await s.commit()

    async def _heal_agent_names(self) -> None:
        """Rename agents still carrying stale names from older catalog revisions to the
        provider's canonical display name, so the UI and routing rationale never show
        raw CLI names like "Codex CLI" or "IBM Bob"."""
        from sqlalchemy import select
        from .catalog import PROVIDERS
        from .models import Agent
        async with self.db.sessionmaker() as s:
            rows = list((await s.execute(select(Agent))).scalars())
            changed = 0
            for a in rows:
                canonical = PROVIDERS.get(a.provider, {}).get("display_name")
                if canonical and a.name != canonical and (
                        a.name in _LEGACY_AGENT_NAMES or not a.name.strip()):
                    a.name = canonical
                    changed += 1
            if changed:
                await s.commit()

    async def shutdown(self) -> None:
        await self.entitlement.stop_polling()
        await self.orchestrator.shutdown()
        await self.bus.stop()
        await self.db.dispose()

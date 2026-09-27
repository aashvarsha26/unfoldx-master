"""Capability- and cost-aware routing with an explanation trail (capability fit, cost, remaining quota).

score = 0.6*capability_fit + 0.2*cost_efficiency + 0.2*budget_headroom (+0.03 Bob tie-break: it is the
conductor). Providers with an open circuit breaker / exhausted quota are excluded. Agents whose CLI is
not installed on this host are excluded from scoring whenever at least one genuinely-available CLI
exists, so routing prefers a real executor over a simulated one; when NO CLI is installed the
high-scoring candidate still wins and the clearly-labelled simulated run is used (demo mode).

Coordination: the orchestrator passes `avoid_provider` (the provider that just took a sibling subtask)
plus a monotonically increasing `rr_counter` over the workspace's dispatches. Within a small score
band (COORD_SPREAD) the pick round-robins across DISTINCT providers by capability order, so parallel
subtasks spread across the connected AIs by strength instead of piling onto the single best scorer."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..catalog import estimate_cost

W_FIT, W_COST, W_QUOTA, BOB_BONUS = 0.6, 0.2, 0.2, 0.03
DEFAULT_CAP_SCORE = 0.3
EST_OUT_TOKENS = 3000  # assumption used only for *pre-dispatch* cost estimates
# Within this score band a candidate counts as "just as good": coordination may
# then prefer a different provider over the absolute best scorer.
COORD_SPREAD = 0.04


@dataclass
class Candidate:
    agent_id: str
    provider: str
    name: str
    capabilities: dict
    pricing: dict
    budget: dict                     # BudgetService state dict
    enabled: bool = True
    quota_requests_remaining: int | None = None
    cli_available: bool = True       # provider CLI installed on this host (adapter.available())
    benched: bool = False            # in failover cooldown: a real run just failed hard (auth/crash)
    authenticated: bool = True       # has a verified credential (API key or confirmed CLI login)


@dataclass
class RouteDecision:
    agent_id: str
    provider: str
    score: float
    est_cost_usd: float
    remaining_usd: float
    rationale: str
    breakdown: dict = field(default_factory=dict)


class RouteFailure(Exception):
    def __init__(self, reason: str, budget_blocked: bool = False):
        super().__init__(reason)
        self.reason, self.budget_blocked = reason, budget_blocked


def est_tokens(description: str) -> tuple[int, int]:
    return max(500, len(description) // 4 + 1500), EST_OUT_TOKENS


def choose(capabilities: list[str], description: str, candidates: list[Candidate], *,
           forced_agent_id: str | None = None, exclude: set[str] | None = None,
           avoid_provider: str | None = None, rr_counter: int = 0) -> RouteDecision:
    exclude = exclude or set()
    tin, tout = est_tokens(description)
    viable: list[tuple[Candidate, float]] = []
    reasons: list[str] = []
    budget_blocked = False
    # Prefer a real, authenticated run:
    # - While any candidate has a CLI on PATH (enabled, not budget-blocked), CLI-less candidates
    #   are excluded so the best *real* executor wins over a simulated one.
    # - While any candidate is authenticated (API key / CLI login confirmed), unauthenticated
    #   candidates are also excluded so the router never routes to a provider that will
    #   immediately fail with an auth error, burn an attempt, and bench itself.
    # Forced / redirect targets are always respected (explicit user override).
    def _usable_cli(c: Candidate) -> bool:
        return c.enabled and c.cli_available \
            and c.budget["breaker_state"] != "open" and c.budget["remaining_usd"] > 0 \
            and (c.quota_requests_remaining is None or c.quota_requests_remaining > 0)
    real_eligible = any(_usable_cli(c) for c in candidates if c.agent_id not in exclude)
    auth_eligible = any(
        _usable_cli(c) and c.authenticated for c in candidates if c.agent_id not in exclude
    )
    for c in candidates:
        if c.agent_id in exclude:
            continue
        if not c.enabled:
            reasons.append(f"{c.name}: disabled")
            continue
        if real_eligible and not forced_agent_id and not c.cli_available:
            reasons.append(f"{c.name}: CLI not installed (no local binary)")
            continue
        if auth_eligible and not forced_agent_id and not c.authenticated:
            reasons.append(f"{c.name}: no credentials (API key or login required)")
            continue
        if c.budget["breaker_state"] == "open" or c.budget["remaining_usd"] <= 0:
            reasons.append(f"{c.name}: budget cap reached")
            budget_blocked = True
            continue
        if c.quota_requests_remaining is not None and c.quota_requests_remaining <= 0:
            reasons.append(f"{c.name}: monthly request quota exhausted")
            budget_blocked = True
            continue
        viable.append((c, estimate_cost(c.pricing, tin, tout)))
    if forced_agent_id:
        viable = [(c, e) for c, e in viable if c.agent_id == forced_agent_id]
        if not viable:
            raise RouteFailure(f"requested agent unavailable ({'; '.join(reasons) or 'not connected/enabled'})", budget_blocked)
    if not viable:
        raise RouteFailure("no agent available: " + ("; ".join(reasons) or "no providers connected"), budget_blocked)

    # Demo-mode fallback ordering: when no genuinely-usable CLI exists, prefer candidates that
    # are NOT in a failover bench (a benched provider just failed hard — re-running it real
    # would be another doomed attempt; only its clearly-labelled simulated run remains).
    non_benched = [(c, e) for c, e in viable if not c.benched]
    if non_benched:
        viable = non_benched

    max_est = max(e for _, e in viable) or 0.0
    scored = []
    for c, est in viable:
        fit = sum(c.capabilities.get(cap, DEFAULT_CAP_SCORE) for cap in capabilities) / max(1, len(capabilities))
        cost_eff = 1.0 - (est / max_est if max_est > 0 else 0.0)
        cap = c.budget["cap_usd"]
        headroom = min(1.0, c.budget["remaining_usd"] / cap) if cap > 0 else 0.0
        total = W_FIT * fit + W_COST * cost_eff + W_QUOTA * headroom + (BOB_BONUS if c.provider == "bob" else 0.0)
        scored.append((total, c, est, {"capability_fit": round(fit, 3), "cost_efficiency": round(cost_eff, 3),
                                        "budget_headroom": round(headroom, 3), "est_cost_usd": round(est, 5),
                                        "score": round(total, 4)}))
    scored.sort(key=lambda t: t[0], reverse=True)
    best_score, best, est, bd = scored[0]

    # ---- coordination: spread near-equal subtasks across distinct providers ----
    # Only for unforced picks; never downgrade an explicitly requested agent.
    if not forced_agent_id and avoid_provider is not None and len(capabilities) <= 2:
        pool = [row for row in scored
                if row[1].provider != avoid_provider and best_score - row[0] <= COORD_SPREAD]
        if pool:
            # order the near-equal pool by strength for this subtask's capabilities;
            # rr_counter rotates through it so consecutive dispatches differ.
            pool.sort(key=lambda row: sum(row[1].capabilities.get(cap, DEFAULT_CAP_SCORE) for cap in capabilities),
                      reverse=True)
            pick = pool[rr_counter % len(pool)]
            best_score, best, est, bd = pick

    others = ", ".join(f"{c.name} {s:.2f}" for s, c, _, _ in scored[1:4])
    why = "explicitly requested" if forced_agent_id else "highest combined score"
    avoid_display = next((c.name for c in candidates if c.provider == avoid_provider), avoid_provider)
    rationale = (f"{best.name} ({why}) for [{', '.join(capabilities)}]: capability fit {bd['capability_fit']:.2f}, "
                 f"est. cost ${est:.3f}, ${best.budget['remaining_usd']:.2f} of ${best.budget['cap_usd']:.2f} budget left"
                 + (f". Runners-up: {others}" if others else "") + (f". Skipped: {'; '.join(reasons)}" if reasons else "")
                 + (f". Coordinated: spread to {best.name} (avoiding {avoid_display})"
                    if avoid_provider is not None and best.provider != avoid_provider else ""))
    bd["candidates"] = [{"agent_id": c.agent_id, "provider": c.provider, **b} for _, c, _, b in scored]
    return RouteDecision(best.agent_id, best.provider, round(best_score, 4), est, best.budget["remaining_usd"], rationale, bd)

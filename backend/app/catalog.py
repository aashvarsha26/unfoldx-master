"""Static provider catalog: capability seed profiles + pricing models + cost normalisation.

IMPORTANT: capability scores and prices below are *seed defaults*, not benchmarks or current vendor
price lists. Both are editable per agent / per provider connection through the API."""
from __future__ import annotations

CAPABILITIES = ["planning", "architecture", "backend", "frontend", "refactor", "testing", "debugging",
                "docs", "security", "data", "devops"]

PROVIDERS: dict[str, dict] = {
    "bob": {
        "display_name": "Bob", "default_model": "bob-2.0",
        "capabilities": {"planning": .95, "architecture": .90, "backend": .80, "refactor": .80, "security": .75,
                         "docs": .70, "testing": .70, "debugging": .70, "frontend": .60, "data": .60, "devops": .60},
        # Bob usage is credit-based; USD conversion is a placeholder to be set from the hackathon account.
        "pricing": {"model": "credits", "usd_per_credit": 0.05, "credits_per_mtok": 20.0},
    },
    "opencode": {
        "display_name": "OpenCode", "default_model": None,
        "capabilities": {"frontend": .88, "refactor": .85, "testing": .82, "backend": .80, "debugging": .80,
                         "architecture": .78, "docs": .75, "security": .70, "data": .70, "planning": .70, "devops": .68},
        "pricing": {"model": "token", "input_per_mtok": 2.0, "output_per_mtok": 8.0},
    },
    "codex": {
        "display_name": "ChatGPT", "default_model": "codex",
        "capabilities": {"backend": .80, "frontend": .75, "testing": .75, "refactor": .70, "debugging": .70,
                         "devops": .70, "architecture": .65, "docs": .60, "security": .60, "data": .60, "planning": .55},
        "pricing": {"model": "token", "input_per_mtok": 1.25, "output_per_mtok": 10.0},
        "note": "Headless mode reported unstable for sustained non-TTY use: best-effort provider.",
    },
    "github_copilot": {
        "display_name": "GitHub Copilot", "default_model": None,
        "capabilities": {"backend": .82, "frontend": .82, "testing": .80, "refactor": .78, "debugging": .78,
                         "docs": .72, "devops": .75, "architecture": .68, "security": .62, "data": .60, "planning": .50},
        # Copilot is seat-priced with the subscription; notional rate keeps a cap meaningful.
        "pricing": {"model": "seat", "monthly_usd": 10.0, "monthly_request_quota": 300, "notional_usd_per_mtok": 2.0},
        "note": "Uses the host's GitHub Copilot CLI login (host session); no API key required.",
    },
    "gemini": {
        "display_name": "Antigravity", "default_model": "gemini",
        "capabilities": {"frontend": .80, "docs": .85, "data": .85, "backend": .70, "testing": .65, "planning": .70,
                         "refactor": .65, "debugging": .65, "architecture": .65, "security": .55, "devops": .60},
        "pricing": {"model": "seat", "monthly_usd": 20.0, "monthly_request_quota": 1000, "notional_usd_per_mtok": 2.0},
    },
}


def normalize_pricing(provider: str, override: dict | None) -> dict:
    p = dict(PROVIDERS[provider]["pricing"])
    p.update(override or {})
    if p.get("model") not in ("token", "credits", "seat"):
        raise ValueError("pricing.model must be token|credits|seat")
    return p


def estimate_cost(pricing: dict, tokens_in: int, tokens_out: int) -> float:
    """One USD-denominated number across fundamentally different pricing models.

    token   : metered API pricing (USD per million input/output tokens)
    credits : vendor credits (credits per Mtok x USD per credit)
    seat    : flat subscription -> *notional* cost from a blended rate, so a cap still bounds runaway use;
              real quota consumption is tracked as requests against monthly_request_quota.
    """
    m = pricing.get("model")
    if m == "token":
        return (tokens_in * pricing.get("input_per_mtok", 0.0) + tokens_out * pricing.get("output_per_mtok", 0.0)) / 1e6
    if m == "credits":
        return (tokens_in + tokens_out) / 1e6 * pricing.get("credits_per_mtok", 0.0) * pricing.get("usd_per_credit", 0.0)
    if m == "seat":
        return (tokens_in + tokens_out) / 1e6 * pricing.get("notional_usd_per_mtok", 0.0)
    return 0.0

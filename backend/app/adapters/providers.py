from __future__ import annotations

import os

from ..config import Settings
from .base import AgentEvent, CliAdapter, _run_auth_probe
from .normalize import agy_event, codex_event, generic_event, opencode_event

_PROBE = "reply with exactly: UAW_AUTH_PROBE_OK"


class BobAdapter(CliAdapter):
    """IBM Bob Shell: Plan-mode decomposition + headless execution (`bob run --output-format stream-json`)."""
    provider, binary = "bob", "bob"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "bob_api_key_env", "bob_cmd", "bob_plan_cmd"

    def parse_json_event(self, obj: dict, state: dict) -> list[AgentEvent]:
        # Bob Shell emits stream-json in the same generic shape (type/text/usage keys).
        # generic_event handles it correctly today; swap this for a bob-specific parser
        # once the exact wire format is confirmed from live runs.
        return generic_event(obj, state)

    async def check_auth(self) -> bool:
        """Return True when an API key is present in the environment (env var set or stored in DB).
        Bob is API-key authenticated, so a key in the environment is sufficient proof of auth."""
        key_env = self.settings.bob_api_key_env  # "BOB_API_KEY"
        if os.environ.get(key_env):
            return True
        # Legacy env var kept for backwards compat with older Railway deployments.
        if os.environ.get("BOBSHELL_API_KEY"):
            return True
        return False


class OpenCodeAdapter(CliAdapter):
    """OpenCode CLI (opencode-ai on npm): `run --format json` NDJSON parsed by opencode_event;
    non-interactive write permission via --auto; plan mode via the built-in read-only `plan` agent."""
    provider, binary = "opencode", "opencode"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "opencode_api_key_env", "opencode_cmd", "opencode_plan_cmd"

    def parse_json_event(self, obj: dict, state: dict) -> list[AgentEvent]:
        return opencode_event(obj, state)

    async def check_auth(self) -> bool:
        # opencode stores its login in host config (`opencode auth login`); a tiny real prompt
        # proves the account is signed in and funded, so routing/entitlement treat it as live.
        return await _run_auth_probe([self.executable(), "run", _PROBE])


class CodexAdapter(CliAdapter):
    """Best-effort: Codex headless mode is reported unstable for sustained non-TTY orchestration."""
    provider, binary = "codex", "codex"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "codex_api_key_env", "codex_cmd", "codex_plan_cmd"

    def parse_json_event(self, obj: dict, state: dict) -> list[AgentEvent]:
        return codex_event(obj, state)

    async def check_auth(self) -> bool:
        return await _run_auth_probe([self.executable(), "exec", "--skip-git-repo-check", "-s", "read-only", _PROBE])


class GeminiAdapter(CliAdapter):
    """Google Antigravity CLI (agy): host-session authenticated, `--output-format stream-json` NDJSON
    normalised by `agy_event` (init / step_update with tool + usage / result)."""
    provider, binary = "gemini", "agy"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "gemini_api_key_env", "gemini_cmd", "gemini_plan_cmd"

    def parse_json_event(self, obj: dict, state: dict) -> list[AgentEvent]:
        return agy_event(obj, state)

    async def check_auth(self) -> bool:
        return await _run_auth_probe([self.executable(), "-p", _PROBE])


class GitHubCopilotAdapter(CliAdapter):
    """GitHub Copilot CLI: host-session authenticated (runs on the user's Copilot subscription).
    Non-interactive mode: `copilot -p {prompt} --allow-all-tools`. Output is plain text lines
    (no JSON stream), so the base parser's log/last_text handling applies; the runner's buffered
    tail becomes the result text."""
    provider, binary = "github_copilot", "copilot"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "copilot_api_key_env", "copilot_cmd", "copilot_plan_cmd"

    async def check_auth(self) -> bool:
        # GITHUB_TOKEN/GH_TOKEN presence is the CLI's documented credential; the org-policy
        # rejection we saw live happens at request time, so a plain env check + probe.
        import os
        if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
            return False
        return await _run_auth_probe([self.executable(), "-p", _PROBE])


def build_adapters(settings: Settings) -> dict[str, CliAdapter]:
    return {a.provider: a for a in (BobAdapter(settings), OpenCodeAdapter(settings),
                                    CodexAdapter(settings), GeminiAdapter(settings),
                                    GitHubCopilotAdapter(settings))}

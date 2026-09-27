from __future__ import annotations

import os

from ..config import Settings
from .base import AgentEvent, CliAdapter, _run_auth_probe
from .normalize import agy_event, bob_event, codex_event, generic_event, opencode_event

_PROBE = "reply with exactly: UAW_AUTH_PROBE_OK"


class BobAdapter(CliAdapter):
    """IBM Bob Shell: Plan-mode decomposition + headless execution.
    Plan mode: `bob run --format json`  (single JSON object on stdout, parsed by parse_plan).
    Execute mode: `bob run --format stream-json`  (NDJSON, parsed by bob_event)."""
    provider, binary = "bob", "bob"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "bob_api_key_env", "bob_cmd", "bob_plan_cmd"

    def parse_json_event(self, obj: dict, state: dict) -> list[AgentEvent]:
        return bob_event(obj, state)

    async def check_auth(self) -> bool:
        """Bob is API-key authenticated. A key present in the environment is sufficient;
        no interactive probe needed (safe to call on Railway)."""
        if os.environ.get(self.settings.bob_api_key_env):   # BOB_API_KEY
            return True
        if os.environ.get("BOBSHELL_API_KEY"):               # legacy compat
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
        # On Railway, OpenCode CLIs are not installed; they run via the local-agent bridge.
        # The bridge's connection state is checked by the orchestrator before dispatching;
        # returning True here when an API key env var is present covers the direct-CLI path
        # (local dev).  On Railway with no CLI, this returns False and the provider is
        # marked as needing the local worker.
        if os.environ.get(self.settings.opencode_api_key_env):
            return True
        return await _run_auth_probe([self.executable(), "run", _PROBE])


class CodexAdapter(CliAdapter):
    """Best-effort: Codex headless mode is reported unstable for sustained non-TTY orchestration."""
    provider, binary = "codex", "codex"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "codex_api_key_env", "codex_cmd", "codex_plan_cmd"

    def parse_json_event(self, obj: dict, state: dict) -> list[AgentEvent]:
        return codex_event(obj, state)

    async def check_auth(self) -> bool:
        # On Railway, Codex runs via the local-agent bridge (user's machine, ChatGPT OAuth).
        # If OPENAI_API_KEY is set in the environment the direct-CLI path is also valid.
        if os.environ.get(self.settings.codex_api_key_env):
            return True
        return await _run_auth_probe(
            [self.executable(), "exec", "--skip-git-repo-check", "-s", "read-only", _PROBE])


class GeminiAdapter(CliAdapter):
    """Google Antigravity CLI (agy): API-key authenticated via GEMINI_API_KEY; `--output-format
    stream-json` NDJSON normalised by `agy_event`.  The `--yes` flag added to every command
    template suppresses interactive confirmation prompts so agy does not hang on Railway."""
    provider, binary = "gemini", "agy"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "gemini_api_key_env", "gemini_cmd", "gemini_plan_cmd"

    def parse_json_event(self, obj: dict, state: dict) -> list[AgentEvent]:
        return agy_event(obj, state)

    async def check_auth(self) -> bool:
        """Antigravity is authenticated via GEMINI_API_KEY (set in the environment or stored in
        the workspace DB).  Checking for the key is sufficient and safe for Railway — the live
        probe would block on stdin on a non-TTY host."""
        if os.environ.get(self.settings.gemini_api_key_env):   # GEMINI_API_KEY
            return True
        return False


class GitHubCopilotAdapter(CliAdapter):
    """GitHub Copilot CLI: host-session authenticated (runs on the user's Copilot subscription).
    Non-interactive mode: `copilot -p {prompt} --allow-all-tools`. Output is plain text lines
    (no JSON stream), so the base parser's log/last_text handling applies; the runner's buffered
    tail becomes the result text."""
    provider, binary = "github_copilot", "copilot"
    api_key_env_attr, cmd_attr, plan_cmd_attr = "copilot_api_key_env", "copilot_cmd", "copilot_plan_cmd"

    async def check_auth(self) -> bool:
        # GITHUB_TOKEN/GH_TOKEN presence is the CLI's documented credential.
        if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
            # Token present: still run the probe if the CLI is available locally, but do
            # not block on it on Railway where copilot is not installed.
            if not self.available():
                return True   # token present + local worker will handle it
            return await _run_auth_probe([self.executable(), "-p", _PROBE])
        return False


def build_adapters(settings: Settings) -> dict[str, CliAdapter]:
    return {a.provider: a for a in (BobAdapter(settings), OpenCodeAdapter(settings),
                                    CodexAdapter(settings), GeminiAdapter(settings),
                                    GitHubCopilotAdapter(settings))}

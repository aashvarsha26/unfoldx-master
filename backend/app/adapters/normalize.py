"""Best-effort normalisation of each CLI's JSON-lines output into AgentEvents.

Claude Code (`--output-format stream-json --verbose`) and Codex (`exec --json`) follow their public
event shapes.  Bob Shell uses `--format stream-json` (execute) / `--format json` (plan).
Whatever Bob's schema changes to, adjust bob_event() ONLY in this file. Non-JSON lines are logged as-is."""
from __future__ import annotations

from typing import Any

from .base import AgentEvent

WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "edit", "write", "write_file", "write_to_file", "replace", "edit_file"}


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _int(*vals: Any) -> int:
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
    return 0


def _usage_event(u: dict | None) -> AgentEvent | None:
    if not isinstance(u, dict):
        return None
    tin = _int(u.get("input_tokens"), u.get("prompt_tokens"), u.get("promptTokenCount"), u.get("inputTokens"))
    tin += _int(u.get("cache_creation_input_tokens"))
    tout = _int(u.get("output_tokens"), u.get("completion_tokens"), u.get("candidatesTokenCount"), u.get("outputTokens"))
    if tin or tout:
        return AgentEvent("usage", tokens_in=tin, tokens_out=tout)
    return None


# Vendor error lines that describe the CLI's own login/credential state. These are normal
# when a host CLI is installed but not signed in: recording them as hard errors fails every
# subtask. Demoted to warning log lines, and the provider is benched via provider_failed()
# so the router re-routes the work to an agent that can actually run.
_CLI_AUTH_PATTERNS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "unauthorized",
    "api key not set",
    "credit balance is too low",
    "insufficient credits",
)

# The CLI is signed in but its headless permission policy refuses the tool call
# (seen live: Antigravity auto-denying Get-ChildItem). Distinct provider status:
# re-authing will NOT fix it; the CLI's permission mode must allow the tools.
_PERMISSION_PATTERNS = (
    "was auto-denied",
    "permission denied by policy",
    "denied by policy",
    "auto-denied",
    "requires approval",
    "permission_mode",
    "tool use denied",
)

def is_cli_auth_error(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in _CLI_AUTH_PATTERNS)


def is_cli_permission_error(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in _PERMISSION_PATTERNS)


def claude_event(obj: dict, state: dict) -> list[AgentEvent]:
    out: list[AgentEvent] = []
    t = obj.get("type")
    if t == "system":
        if obj.get("subtype") == "init":
            out.append(AgentEvent("log", text=f"session started (model: {obj.get('model', 'unknown')})"))
    elif t == "assistant":
        msg = obj.get("message") or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and _s(block.get("text")).strip():
                state["last_text"] = block["text"]
                out.append(AgentEvent("log", text=block["text"]))
            elif block.get("type") == "tool_use":
                name, inp = _s(block.get("name")), block.get("input") or {}
                path = _s(inp.get("file_path")) or _s(inp.get("path"))
                detail = path or _s(inp.get("command"))[:160] or _s(inp.get("pattern"))
                out.append(AgentEvent("log", text=f"tool: {name} {detail}".strip()))
                if name in WRITE_TOOLS and path:
                    out.append(AgentEvent("file", files=[path]))
        mid = msg.get("id")
        seen = state.setdefault("seen_msg_ids", set())
        if mid is None or mid not in seen:  # the same message can be echoed once per content block
            if mid is not None:
                seen.add(mid)
            ue = _usage_event(msg.get("usage"))
            if ue:
                out.append(ue)
    elif t == "result":
        if isinstance(obj.get("total_cost_usd"), (int, float)):
            out.append(AgentEvent("cost_total", cost_usd=float(obj["total_cost_usd"])))
        if obj.get("is_error"):
            out.append(AgentEvent("error", text=_s(obj.get("result")) or "Claude Code reported an error"))
        else:
            out.append(AgentEvent("result", text=_s(obj.get("result")) or state.get("last_text", "")))
    return out


def codex_event(obj: dict, state: dict) -> list[AgentEvent]:
    out: list[AgentEvent] = []
    t = _s(obj.get("type"))
    item = obj.get("item") or {}
    if t in ("item.completed", "item.started") and isinstance(item, dict):
        it = _s(item.get("type"))
        if t == "item.completed":
            if it == "agent_message" and _s(item.get("text")).strip():
                state["last_text"] = item["text"]
                out.append(AgentEvent("log", text=item["text"]))
            elif it == "command_execution":
                out.append(AgentEvent("log", text=f"$ {_s(item.get('command'))[:200]}"))
            elif it == "file_change":
                paths = [c.get("path") for c in item.get("changes") or [] if isinstance(c, dict) and c.get("path")]
                if paths:
                    out.append(AgentEvent("log", text="files changed: " + ", ".join(paths)))
                    out.append(AgentEvent("file", files=paths))
            elif it == "reasoning" and _s(item.get("text")).strip():
                out.append(AgentEvent("log", text=f"(reasoning) {item['text'][:300]}"))
    elif t == "turn.completed":
        ue = _usage_event(obj.get("usage"))
        if ue:
            out.append(ue)
        out.append(AgentEvent("result", text=state.get("last_text", "")))
    elif t in ("turn.failed", "error"):
        err = obj.get("error")
        msg = _s(obj.get("message")) or (_s(err.get("message")) if isinstance(err, dict) else _s(err))
        out.append(AgentEvent("error", text=msg or "Codex reported an error"))
    return out


def agy_event(obj: dict, state: dict) -> list[AgentEvent]:
    """Google Antigravity CLI (agy) `--output-format stream-json` (NDJSON): init / step_update (tool,
    agent_response, usage) / result. Mirrors the schema: one `init` event first, `step_update` events per
    model step, exactly one `result` event last."""
    out: list[AgentEvent] = []
    t = _s(obj.get("event"))
    if t == "init":
        init = obj.get("init") or {}
        mode = _s(init.get("permission_mode")) or "unknown"
        out.append(AgentEvent("log", text=f"session started (permission mode: {mode})"))
    elif t == "step_update":
        st = obj.get("step_update") or {}
        kind, sstate = _s(st.get("step_type")), _s(st.get("state"))
        if kind == "tool":
            info = st.get("tool_info") or {}
            name = _s(info.get("name")) or _s(st.get("tool_name"))
            params = info.get("parameters") or {}
            path = _s(params.get("FilePath")) or _s(params.get("file_path")) or _s(params.get("path"))
            cmd = _s(params.get("CommandLine"))
            detail = path or cmd[:160]
            out.append(AgentEvent("log", text=f"tool: {name} {detail}".strip()))
            if name in WRITE_TOOLS and path:
                out.append(AgentEvent("file", files=[path]))
            err = info.get("error")
            if isinstance(err, dict) and (_s(err.get("message")) or _s(err.get("type"))):
                out.append(AgentEvent("error", text=_s(err.get("message")) or _s(err.get("type"))))
        elif kind == "agent_response":
            delta = _s(st.get("text_delta"))
            if delta:
                state["last_text"] = state.get("last_text", "") + delta
                out.append(AgentEvent("log", text=delta))
        if sstate == "DONE":
            seen = state.setdefault("agy_usage_steps", set())
            idx = st.get("step_index")
            ue = _agy_usage(st.get("usage"))
            if ue and idx is not None and idx not in seen:
                seen.add(idx)
                state["agy_usage_emitted"] = True
                out.append(ue)
    elif t == "result":
        res = obj.get("result") or {}
        # agy commonly repeats cumulative usage in the final result after already
        # reporting the same usage on the DONE step. Count it only when no step
        # usage was emitted.
        if not state.get("agy_usage_emitted"):
            ue = _agy_usage(res.get("usage"))
            if ue:
                out.append(ue)
        status = _s(res.get("status"))
        if status and status != "SUCCESS":
            msg = _s(res.get("error")) or f"agy run ended with status {status}"
            out.append(AgentEvent("error", text=msg))
        else:
            text = _s(res.get("response")) or state.get("last_text", "")
            out.append(AgentEvent("result", text=text))
    return out


def _agy_usage(u: dict | None) -> AgentEvent | None:
    """agy usage carries input/output/thinking tokens; thinking counts toward model output."""
    if not isinstance(u, dict):
        return None
    tin = _int(u.get("input_tokens"), u.get("prompt_tokens"))
    tout = _int(u.get("output_tokens")) + _int(u.get("thinking_tokens"))
    if tin or tout:
        return AgentEvent("usage", tokens_in=tin, tokens_out=tout)
    return None


def opencode_event(obj: dict, state: dict) -> list[AgentEvent]:
    """opencode `run --format json` NDJSON: {type: step_start|text|tool_use|step_finish, part: {...}}.
    step_finish carries authoritative token counts (part.tokens) and cost. tool_use parts carry
    tool name + input (file_path for read/write tools, command for bash)."""
    out: list[AgentEvent] = []
    t = _s(obj.get("type"))
    part = obj.get("part") or {}
    pt = _s(part.get("type"))
    if t == "text" or (t == "step_start" and pt == "text") or pt == "text":
        txt = _s(part.get("text"))
        if txt.strip() and not txt.startswith("<"):
            state["last_text"] = txt
            out.append(AgentEvent("log", text=txt))
    elif t == "tool_use" or pt == "tool":
        name = _s(part.get("tool")) or _s(part.get("name"))
        inp = part.get("state") or {}
        inp = inp.get("input") or inp if isinstance(inp, dict) else {}
        path = _s(inp.get("filePath")) or _s(inp.get("file_path")) or _s(inp.get("path"))
        cmd = _s(inp.get("command"))
        detail = path or cmd[:160]
        out.append(AgentEvent("log", text=f"tool: {name} {detail}".strip()))
        if name in ("write", "edit", "patch", "multiedit", "write_file", "edit_file") and path:
            out.append(AgentEvent("file", files=[path]))
        elif name == "bash" and cmd:
            for token in cmd.replace(";", " ").replace("|", " ").replace(">", " ").split():
                if token in (">", ">>") or not token or token.startswith("-"):
                    continue
                if token.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".json", ".md",
                                   ".sql", ".yml", ".yaml", ".toml", ".sh", ".txt")) and "/" not in token[:2]:
                    state.setdefault("bash_files", set()).add(token)
                elif token in ("python", "python3", "pip", "npm", "npx", "node"):
                    break
    elif t == "step_finish" or pt == "step-finish":
        tok = part.get("tokens") or {}
        tin = _int(tok.get("input"), tok.get("inputTokens"), tok.get("prompt_tokens"))
        tin += _int((tok.get("cache") or {}).get("read") if isinstance(tok.get("cache"), dict) else 0)
        tout = _int(tok.get("output"), tok.get("outputTokens"), tok.get("completion_tokens"))
        tout += _int(tok.get("reasoning"))
        ue = _usage_event({"input_tokens": tin, "output_tokens": tout})
        if ue:
            out.append(ue)
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and cost > 0:
            out.append(AgentEvent("cost_total", cost_usd=float(cost)))
        if part.get("reason") == "stop":
            bf = state.pop("bash_files", None)
            if bf:
                out.append(AgentEvent("file", files=sorted(bf)))
            out.append(AgentEvent("result", text=state.get("last_text", "")))
    return out


def bob_event(obj: dict, state: dict) -> list[AgentEvent]:
    """IBM Bob Shell `--format stream-json` (execute mode) NDJSON normaliser.

    Bob emits a stream of JSON lines.  The documented keys observed in practice:
      {"type": "text",    "text": "..."}          -> log line / accumulate last_text
      {"type": "tool",    "name": "...", "input": {...}}  -> tool call
      {"type": "result",  "text": "..."}          -> final answer
      {"type": "usage",   "input_tokens": N, "output_tokens": N}
      {"type": "error",   "message": "..."}

    `generic_event` is used as a catch-all fallback for any shape not explicitly handled,
    keeping this parser forward-compatible with Bob schema changes.
    """
    out: list[AgentEvent] = []
    t = _s(obj.get("type")).lower()

    if t == "text":
        txt = _s(obj.get("text"))
        if txt.strip():
            state["last_text"] = txt
            out.append(AgentEvent("log", text=txt))
        return out

    if t == "tool":
        name = _s(obj.get("name"))
        inp = obj.get("input") or {}
        path = (_s(inp.get("file_path")) or _s(inp.get("path")) or
                _s(inp.get("filePath")) or _s(inp.get("filename")))
        cmd = _s(inp.get("command"))
        detail = path or cmd[:160]
        out.append(AgentEvent("log", text=f"tool: {name} {detail}".strip()))
        if name in WRITE_TOOLS and path:
            out.append(AgentEvent("file", files=[path]))
        return out

    if t == "result":
        txt = _s(obj.get("text")) or state.get("last_text", "")
        out.append(AgentEvent("result", text=txt))
        return out

    if t in ("error", "fatal"):
        msg = _s(obj.get("message")) or _s(obj.get("text")) or "Bob reported an error"
        out.append(AgentEvent("error", text=msg))
        return out

    if t == "usage":
        ue = _usage_event(obj)
        if ue:
            out.append(ue)
        return out

    # Unknown Bob event type — fall through to the generic handler so new event
    # types don't silently disappear from the UI.
    return generic_event(obj, state)


def generic_event(obj: dict, state: dict) -> list[AgentEvent]:
    out: list[AgentEvent] = []
    t = _s(obj.get("type")).lower()
    text = ""
    for key in ("text", "message", "content", "delta", "output", "line"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            text = v
            break
        if isinstance(v, dict) and _s(v.get("text")).strip():
            text = v["text"]
            break
    if t in ("error", "fatal"):
        out.append(AgentEvent("error", text=text or "provider reported an error"))
        return out
    if text and t not in ("result", "final", "done", "complete", "completed"):
        state["last_text"] = text
        out.append(AgentEvent("log", text=text))
    path = _s(obj.get("file_path")) or _s(obj.get("path"))
    files = obj.get("files") if isinstance(obj.get("files"), list) else ([path] if path else [])
    files = [f for f in files if isinstance(f, str)]
    if files and t in ("file", "file_edit", "file_write", "tool_use", "tool_call", "edit", "write"):
        out.append(AgentEvent("file", files=files))
    ue = _usage_event(obj.get("usage") or obj.get("usageMetadata") or obj.get("stats"))
    if ue:
        out.append(ue)
    for key in ("total_cost_usd", "cost_usd"):
        if isinstance(obj.get(key), (int, float)):
            out.append(AgentEvent("cost_total", cost_usd=float(obj[key])))
            break
    if t in ("result", "final", "done", "complete", "completed"):
        out.append(AgentEvent("result", text=text or state.get("last_text", "")))
    return out

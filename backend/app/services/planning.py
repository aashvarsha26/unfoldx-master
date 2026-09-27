"""Plan parsing/validation, the heuristic fallback planner, and handoff-object parsing."""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from ..catalog import CAPABILITIES

MAX_SUBTASKS = 8

HANDOFF_INSTRUCTIONS = (
    "When you finish, END your reply with one fenced ```json block containing exactly these keys: "
    '{"summary": str, "decisions": [str], "constraints": [str], "rejected_approaches": [str], '
    '"files_touched": [str]}. decisions = choices you made and why; constraints = limits you discovered '
    "that the next agent must respect; rejected_approaches = things you tried or considered and dropped."
)

PLAN_START, PLAN_END = "### REQUEST", "### END REQUEST"


class PlanSubtask(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    capabilities: list[str] = []
    files: list[str] = []
    depends_on: list[int] = []


class Plan(BaseModel):
    rationale: str = ""
    subtasks: list[PlanSubtask] = Field(min_length=1)


# ---------------------------------------------------------------------------------------------
def _json_candidates(text: str) -> list[dict]:
    """All JSON objects found in text: try the whole text first (Bob --format json outputs a
    single bare JSON object with no fencing), then fenced blocks, then balanced-brace scans."""
    found: list[dict] = []

    # Fast path: Bob plan mode writes one clean JSON object to stdout, nothing else.
    # Also handles NDJSON: the last non-empty line that is a valid JSON plan object wins.
    for line in reversed(text.strip().splitlines()):
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                v = json.loads(s)
                if isinstance(v, dict) and "subtasks" in v:
                    found.append(v)
                    break
            except json.JSONDecodeError:
                pass

    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I):
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict) and v not in found:
                found.append(v)
        except json.JSONDecodeError:
            pass
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(text[i:j + 1])
                        if isinstance(v, dict) and v not in found:
                            found.append(v)
                    except json.JSONDecodeError:
                        pass
                    break
            j += 1
        i = j + 1 if depth == 0 and j < n else i + 1
    return found


def _clean_path(p: str) -> str | None:
    p = p.strip().replace("\\", "/").lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    if not p or ".." in p.split("/") or len(p) > 200:
        return None
    return p


def parse_plan(text: str) -> Plan | None:
    """Validate Bob's plan output and sanitise it (known capabilities, safe paths, acyclic deps)."""
    for cand in _json_candidates(text):
        if "subtasks" not in cand:
            continue
        try:
            plan = Plan.model_validate(cand)
        except ValidationError:
            continue
        plan.subtasks = plan.subtasks[:MAX_SUBTASKS]
        for i, st in enumerate(plan.subtasks):
            st.capabilities = [c for c in dict.fromkeys(x.lower() for x in st.capabilities) if c in CAPABILITIES] or ["backend"]
            st.files = [f for f in (_clean_path(x) for x in st.files) if f][:20]
            st.depends_on = sorted({d for d in st.depends_on if isinstance(d, int) and 0 <= d < i})
        return plan
    return None


def parse_handoff(text: str) -> dict | None:
    keys = {"decisions", "constraints", "rejected_approaches", "files_touched"}
    for cand in reversed(_json_candidates(text)):
        if keys & cand.keys():
            def lst(k):
                v = cand.get(k)
                return [str(x)[:500] for x in v][:50] if isinstance(v, list) else []
            return {"summary": str(cand.get("summary", ""))[:2000], "decisions": lst("decisions"),
                    "constraints": lst("constraints"), "rejected_approaches": lst("rejected_approaches"),
                    "files_touched": lst("files_touched")}
    return None


# ---------------------------------------------------------------------------------------------
# Keywords are matched with \b word boundaries so substrings like "bridge-test.txt"
# or "attest" do NOT trigger the "testing" rule.  Each tuple entry is a keyword
# (single word / phrase) that must appear as a whole word / phrase in the request.
_RULES: list[tuple[str, tuple[str, ...], str, list[str]]] = [
    # capability, keywords, title, default claimed paths
    ("docs", ("presentation", "slides", "slide deck", "ppt", "pitch deck"), "Create the presentation", ["slides.html"]),
    ("architecture", ("architecture", "design the", "data model", "schema"), "Design the approach and data model", ["docs/design/**"]),
    ("backend", ("api", "endpoint", "server", "backend", "database", "fastapi", "django", "flask", "express", "sql", "auth", "login"), "Implement backend logic", ["backend/**"]),
    ("data", ("csv", "etl", "pandas", "dataset", "analysis", "analytics"), "Build the data pipeline", ["data/**"]),
    ("refactor", ("refactor", "clean up", "cleanup", "rename", "restructure"), "Refactor existing code", ["src/**"]),
    ("debugging", ("bug", "fix", "crash", "error", "broken"), "Diagnose and fix the defect", ["src/**"]),
    ("security", ("security", "vulnerab", "sanitiz", "xss", "csrf", "harden"), "Security review and hardening", ["backend/**"]),
    ("frontend", ("ui", "ux", "page", "component", "frontend", "react", "next.js", "css", "layout", "dashboard", "form", "button", "website", "web site", "web page", "landing", "homepage", "site", "gallery", "menu"), "Build the user interface (UI/UX)", ["frontend/**"]),
    ("devops", ("docker", "deploy", "ci/cd", "pipeline", "kubernetes", "preview server", "dev server", "local server", "serve", "host it", "run it locally"), "Containerise and configure deployment", ["Dockerfile", "docker-compose.yml", ".github/**"]),
    # "test" / "tests" require a full word boundary: "bridge-test.txt" must not match.
    ("testing", ("tests", "pytest", "coverage", "unit test", "write tests", "run tests", "qa"), "Write and run tests", ["tests/**"]),
    ("docs", ("readme", "docs", "documentation", "document "), "Write documentation", ["docs/**", "README.md"]),
]
_EXT_TO_CAP = {".py": "backend", ".go": "backend", ".java": "backend", ".rs": "backend", ".sql": "backend",
               ".tsx": "frontend", ".jsx": "frontend", ".css": "frontend", ".html": "frontend", ".vue": "frontend",
               ".md": "docs", ".yml": "devops", ".yaml": "devops", ".csv": "data"}


def heuristic_plan(request: str) -> dict:
    """Keyword-based decomposition. Used ONLY as a fallback when Bob is unavailable/unparseable, and by
    the simulator. Real decomposition is Bob's Plan mode."""
    low = request.lower()
    chosen = [r for r in _RULES if any(re.search(r"\b" + re.escape(k.strip()), low) for k in r[1])]
    if not chosen:
        chosen = [_RULES[1]]
    explicit = [p for p in re.findall(r"[\w./-]+\.[A-Za-z0-9]{1,5}\b", request) if "/" in p or p.count(".") == 1]
    explicit = [p for p in (_clean_path(x) for x in explicit) if p and not p.startswith("http")]
    subtasks: list[dict] = []
    for cap, _, title, files in chosen:
        mine = [p for p in explicit if _EXT_TO_CAP.get(_ext(p)) == cap]
        if cap == "docs" and not mine:
            # Artifact requests ("a presentation (slides.html)") name their output file directly:
            # claim exactly that file so the executor builds the artifact the user asked for.
            mine = [p for p in explicit if _ext(p) in (".html", ".pptx", ".md")]
        desc = f"{title} for: {request.strip()[:400]}"
        if cap == "docs" and len(chosen) == 1:
            desc = request.strip()[:2000]  # the artifact spec IS the work order
        subtasks.append({"title": title, "description": desc,
                         "capabilities": [cap], "files": mine or list(files), "depends_on": []})
    for i, st in enumerate(subtasks):  # tests/docs run after the implementation work they describe
        if st["capabilities"][0] in ("testing", "docs", "devops"):
            st["depends_on"] = [j for j in range(i) if subtasks[j]["capabilities"][0] not in ("testing", "docs", "devops")]
    # A web build is not demoable without something to open: when the plan renders UI/artifact
    # files, Bob appends a dedicated preview-server subtask that depends on that work.
    ui_idx = [i for i, st in enumerate(subtasks)
              if st["capabilities"][0] == "frontend" or any(f.endswith((".html", ".htm")) for f in st["files"])]
    if ui_idx and not any(st["capabilities"][0] == "devops" for st in subtasks):
        subtasks.append({
            "title": "Set up the preview server",
            "description": "Create serve.py (python -m http.server wrapper binding 127.0.0.1 on a free port, serving this repo's "
                           "built files) plus a short 'Preview' section in README.md with the exact command to run. Verify the "
                           "server starts and responds 200 on / before finishing.",
            "capabilities": ["devops"], "files": ["serve.py", "README.md"],
            "depends_on": ui_idx,
        })
    return {"rationale": "Heuristic decomposition by capability keywords (Bob Plan mode output was unavailable).",
            "subtasks": subtasks[:MAX_SUBTASKS]}


def _ext(p: str) -> str:
    return "." + p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""


def build_plan_prompt(request: str, attachments: list[str], agents: list[dict],
                      attachment_texts: list[tuple[str, str]] | None = None) -> str:
    agent_lines = "\n".join(f"- {a['provider']}: strengths " + ", ".join(
        k for k, v in sorted(a["capabilities"].items(), key=lambda kv: -kv[1])[:4]) for a in agents)
    att_sections = ""
    if attachment_texts:
        for name, text in attachment_texts:
            if text:
                att_sections += (f"\nAttached file '{name}' (the design/spec to decompose against — honour it exactly):\n"
                                 f"--- BEGIN {name} ---\n{text}\n--- END {name} ---\n")
            else:
                att_sections += f"\nAttached file (not readable as text): {name}\n"
    elif attachments:
        att_sections = f"\nAttached files: {', '.join(attachments)}\n"
    return (
        "You are Bob in Plan mode, the conductor of a multi-agent development workspace. Decompose the request "
        f"into 1-{MAX_SUBTASKS} subtasks that independent coding agents can execute. Subtasks that edit the same "
        "files must not run in parallel, so list precise `files` (globs allowed) each subtask will modify.\n"
        "Respond with ONLY one JSON object, no prose:\n"
        '{"rationale": str, "subtasks": [{"title": str, "description": str, "capabilities": [str], '
        '"files": [str], "depends_on": [int]}]}\n'
        f"capabilities must come from: {', '.join(CAPABILITIES)}. depends_on lists indices of EARLIER subtasks.\n"
        f"Connected agents:\n{agent_lines}\n"
        + att_sections
        + f"{PLAN_START}\n{request}\n{PLAN_END}")


def extract_request(prompt: str) -> str:
    if PLAN_START in prompt and PLAN_END in prompt:
        return prompt.split(PLAN_START, 1)[1].split(PLAN_END, 1)[0].strip()
    return prompt

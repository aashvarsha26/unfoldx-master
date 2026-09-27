# UNFOLD X local OpenCode + GitHub Copilot

OpenCode and GitHub Copilot are **agent runtimes** in UNFOLD X, not model providers.

- **OpenCode** = local coding-agent runtime. It can call different LLM providers/models.
- **GitHub Copilot CLI** = local coding-agent runtime. It can use GitHub-hosted Copilot models or BYOK models.
- **Model** = the actual LLM selected underneath the runtime.
- **UNFOLD X provider** = the execution/runtime lane that owns a task, permissions, files, telemetry, and handoff.

The local worker keeps credentials on your Windows machine. Railway only sends prompts and receives stdout.

## 1. OpenCode

Install OpenCode on Windows using the current supported installer/package method from https://opencode.ai/docs.

Then, from PowerShell:

```powershell
opencode --version
opencode auth login
opencode models
```

Use `opencode auth login` to configure the LLM provider(s) you want OpenCode to use. OpenCode stores credentials locally and can also load provider credentials from the environment/.env.

Pick a real model with:

```powershell
opencode models
```

For UNFOLD X, you do **not** need to put an OpenCode API key into Railway.

Test the exact local checkout:

```powershell
cd C:\path\to\your\unfoldx-checkout
opencode run --format json --auto "Read this repository and summarize its architecture."
```

If that works, configure the bridge:

```powershell
$env:UNFOLDX_BACKEND_WS="wss://unfoldx-master-production.up.railway.app"
$env:UNFOLDX_AGENT_BRIDGE_TOKEN="<same token as Railway>"
$env:UNFOLDX_LOCAL_REPO="C:\path\to\your\unfoldx-checkout"
$env:UNFOLDX_LOCAL_PROVIDER="opencode"

python backend\scripts\local_provider_worker.py
```

Leave that terminal running.

### Choosing the OpenCode model

The worker accepts a model passed by UNFOLD X. The value must be an actual OpenCode model id such as `provider/model`, not `opencode`.

If UNFOLD X has no explicit model selection, OpenCode uses its configured/default model.

## 2. GitHub Copilot CLI

Install the current Copilot CLI:

```powershell
npm install -g @github/copilot
```

Verify:

```powershell
copilot --version
```

Authenticate:

```powershell
copilot login
```

Complete the browser/device-code GitHub authentication flow.

Then test it inside your actual checkout:

```powershell
cd C:\path\to\your\unfoldx-checkout
copilot -p "Read this repository and summarize its architecture." --allow-all-tools
```

For UNFOLD X, the worker runs Copilot non-interactively with tool/path/URL permissions enabled because it is an automated executor. This means it has the same practical access as your local user, so only point it at a checkout you trust.

You can see/select available models interactively with `/model`, or specify one with `--model`.

Then configure the bridge:

```powershell
$env:UNFOLDX_BACKEND_WS="wss://unfoldx-master-production.up.railway.app"
$env:UNFOLDX_AGENT_BRIDGE_TOKEN="<same token as Railway>"
$env:UNFOLDX_LOCAL_REPO="C:\path\to\your\unfoldx-checkout"
$env:UNFOLDX_LOCAL_PROVIDER="github_copilot"

python backend\scripts\local_provider_worker.py
```

Leave that terminal running.

## 3. You can run multiple workers

You can run all local agents simultaneously in separate PowerShell windows:

### Codex
```powershell
$env:UNFOLDX_LOCAL_PROVIDER="codex"
python backend\scripts\codex_worker.py
```

### OpenCode
```powershell
$env:UNFOLDX_LOCAL_PROVIDER="opencode"
python backend\scripts\local_provider_worker.py
```

### GitHub Copilot
```powershell
$env:UNFOLDX_LOCAL_PROVIDER="github_copilot"
python backend\scripts\local_provider_worker.py
```

All three can point at the same local checkout, but UNFOLD X should use its existing conflict/file-ownership system to avoid simultaneous edits to the same paths.

## 4. What UNFOLD X should call them

Do **not** model the architecture as:

```
OpenCode = model
Copilot = model
```

Use:

```
                    UNFOLD X
                        |
             capability-aware router
                        |
          +-------------+-------------+
          |             |             |
       Bob/CLI      OpenCode       Copilot CLI
          |             |             |
       Bob model    selected LLM    selected LLM
          |             |             |
       API key      local auth      GitHub auth/BYOK
```

For your product UI, the clean terminology is:

- **Agent / Runtime:** Bob, Antigravity, OpenCode, Codex, GitHub Copilot
- **Model:** the actual model used by that runtime
- **Auth:** API key, ChatGPT login, GitHub login, etc.
- **Execution:** Railway-hosted or local-worker
- **Capabilities:** what UNFOLD X uses for routing

That gives you a much stronger architecture than treating every CLI as if it were itself an LLM.

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# Node.js 20 LTS is required by IBM Bob Shell and the Codex CLI (both npm-based).
# Node 24 is not yet widely available on all Debian slim variants; 20 LTS is stable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- IBM Bob Shell ---
# The official installer URL is https://bob.ibm.com/download/bobshell.sh
# If this fails at build time (network restriction or URL change), the build
# will continue but `bob` will not be on PATH. The backend will fall back to
# the heuristic planner; set ALLOW_SIMULATION=0 to surface failures explicitly.
# MANUAL: verify the correct installer URL from https://ibm.com/products/bob
RUN curl -fsSL https://bob.ibm.com/download/bobshell.sh | bash \
    && command -v bob \
    && bob --version \
    || echo "WARNING: IBM Bob Shell install failed — bob will not be available at runtime"

# --- OpenAI Codex CLI ---
RUN npm install -g @openai/codex \
    && command -v codex \
    && codex --version \
    || echo "WARNING: Codex CLI install failed — codex will not be available at runtime"

COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY schema ./schema
RUN useradd -m uaw && mkdir -p /srv/data && chown -R uaw /srv

# --- Google Antigravity CLI (agy) ---
# MANUAL: Verify the installer URL before deploying. The domain antigravity.google
# may not be the final distribution URL. Check https://cloud.google.com/products or
# the Antigravity CLI GitHub repo for the current install instructions.
# If this fails, `agy` will not be available and the Gemini provider will simulate.
USER uaw
ENV HOME=/home/uaw PATH=/home/uaw/.local/bin:$PATH
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && mkdir -p /home/uaw/.gemini/antigravity-cli \
    && printf '{"modelProvider":"gemini"}\n' > /home/uaw/.gemini/antigravity-cli/settings.json \
    && agy --version \
    || echo "WARNING: Antigravity CLI (agy) install failed — gemini provider will not be available at runtime"

EXPOSE 8000

# IMPORTANT for production on Railway:
#   - Set ALLOW_SIMULATION=0 so missing CLIs fail explicitly instead of silently simulating.
#   - Set SECRET_KEY to a stable random value (openssl rand -hex 32) — never let it regenerate on restart.
#   - Set DATABASE_URL to a Postgres connection string and mount a volume at /srv/data,
#     otherwise the SQLite DB and encrypted API keys are wiped on every redeploy.
#   - Set CORS_ORIGINS to your frontend Railway domain(s), comma-separated.
#   - Set provider API keys: BOB_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY as Railway env vars.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

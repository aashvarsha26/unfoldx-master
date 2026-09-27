FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

# Node.js 24+ is required by IBM Bob Shell. Codex CLI is installed via npm.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://bob.ibm.com/download/bobshell.sh | bash \
    && npm install -g @openai/codex \
    && command -v bob \
    && command -v codex \
    && bob --version \
    && codex --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY schema ./schema
RUN useradd -m uaw && mkdir -p /srv/data && chown -R uaw /srv

# Antigravity CLI is installed as the same non-root user that runs the backend.
# Its Gemini API-key provider is selected through this settings file; the actual
# GEMINI_API_KEY is injected by Railway at runtime and is never baked into the image.
USER uaw
ENV HOME=/home/uaw PATH=/home/uaw/.local/bin:$PATH
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && mkdir -p /home/uaw/.gemini/antigravity-cli \
    && printf '{"modelProvider":"gemini"}\n' > /home/uaw/.gemini/antigravity-cli/settings.json \
    && agy --version

EXPOSE 8000
# Bob Shell, OpenAI Codex CLI, and Antigravity CLI are installed in the image.
# Set ALLOW_SIMULATION=0 in production so missing providers fail instead of simulating.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

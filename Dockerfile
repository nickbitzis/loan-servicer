# Build stage: resolve and install dependencies with uv into a virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Runtime stage: slim image, non-root user, only the venv and code.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 servicer
WORKDIR /app
COPY --from=build --chown=servicer:servicer /app /app
# the audit log is written under /app/var at runtime
RUN mkdir -p /app/var && chown servicer:servicer /app/var
USER servicer
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8880 LS_AUDIT=/app/var/audit.jsonl
EXPOSE 8880
# the service simulates 24 months for 5,000 loans at startup (about 20 seconds), hence the start period
HEALTHCHECK --interval=15s --timeout=3s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8880/healthz', timeout=2)"
CMD ["loanservicer", "serve"]

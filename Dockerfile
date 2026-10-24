# Runs the CLI in a container:
#   docker build -t strom-agent .
#   docker run --rm -it --env-file .env strom-agent ask "..."
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

WORKDIR /app
# Install the locked dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY evals ./evals
COPY data/fixtures ./data/fixtures
RUN uv sync --locked --no-dev

# Do not run as root.
RUN useradd --create-home app && chown -R app /app
USER app
ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["strom-agent"]

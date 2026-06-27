# ai-wiki service image — reproducible, host-isolated read/serve of an OKF bundle.
# Mount the bundle read-only at /bundle and pass AIWIKI_TOKEN at run time:
#   docker build -t ai-wiki .
#   docker run -d --name ai-wiki --restart unless-stopped \
#     -p 127.0.0.1:8787:8787 -v /path/to/bundle:/bundle:ro \
#     -e AIWIKI_TOKEN=... -e AIWIKI_DISABLE=ingest -e AIWIKI_CURATE=off ai-wiki
FROM python:3.12-slim

# uv (dependency manager) from its official image — fast, no pip bootstrap.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --extra service --frozen --no-dev

# Bundle is mounted at /bundle (read-only); bind all interfaces inside the container
# (publish only to host loopback via `-p 127.0.0.1:8787:8787`).
ENV AIWIKI_BUNDLE=/bundle \
    AIWIKI_HOST=0.0.0.0 \
    AIWIKI_PORT=8787
EXPOSE 8787

CMD ["uv", "run", "--no-dev", "python", "-m", "aiwiki.service"]

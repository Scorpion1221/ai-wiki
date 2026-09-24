# ai-wiki service image — reproducible, host-isolated read/serve of OKF bundles.
# Mount a dir of bundles (one per subdir) read-only at /bundles and pass AIWIKI_TOKEN:
#   docker build -t ai-wiki .
#   docker run -d --name ai-wiki --restart unless-stopped \
#     -p 127.0.0.1:8787:8787 -v /path/to/bundles:/bundles:ro \
#     -e AIWIKI_TOKEN=... -e AIWIKI_DISABLE=ingest,audit,create,delete,changesets,workspace,maint,admin \
#     -e AIWIKI_CURATE=off ai-wiki
# A mirror serves reads only: the writer answers every other route (design §2.1), so a request
# routed here by mistake is refused rather than answered from the mirror's clone.
# Or share the writer's principals file instead of AIWIKI_TOKEN; the mirror's routes only
# need the `read` scope. Mount the directory (a single-file bind mount pins the old inode
# when the file is replaced), check the edited file as the container sees it, then send SIGHUP
# (a refused reload keeps the old principals and only logs):
#     -v /etc/ai-wiki:/etc/ai-wiki:ro -e AIWIKI_PRINCIPALS=/etc/ai-wiki/principals.json
#   docker exec ai-wiki uv run --no-dev python -m aiwiki.service.auth /etc/ai-wiki/principals.json
#   docker kill -s HUP ai-wiki
FROM python:3.12-slim

# Health revision reporting and the writer transaction path both require Git. The container
# runs as root and the clones under /bundles belong to whoever pulls them on the host, so trust
# them: otherwise Git refuses a clone it does not own ("dubious ownership") and /health reports
# git_revision null. The `/*` prefix match needs Git 2.46 or later.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '/bundles/*'

# uv (dependency manager) from its official image — fast, no pip bootstrap.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --extra service --frozen --no-dev

# Bundles are mounted at /bundles (read-only), one bundle per subdirectory; bind all
# interfaces inside the container (publish only to host loopback via `-p 127.0.0.1:8787:8787`).
ENV AIWIKI_BUNDLES=/bundles \
    AIWIKI_HOST=0.0.0.0 \
    AIWIKI_PORT=8787 \
    AIWIKI_CURATE=off \
    AIWIKI_DISABLE=ingest,audit,create,delete,changesets,workspace,maint,admin
# Deployed revision reported as /health "build"; declared after `uv sync` so a new revision
# keeps the dependency layer cached:
#   docker build --build-arg AIWIKI_BUILD_COMMIT=$(git rev-parse HEAD) -t ai-wiki .
ARG AIWIKI_BUILD_COMMIT=
ENV AIWIKI_BUILD_COMMIT=${AIWIKI_BUILD_COMMIT}
EXPOSE 8787

CMD ["uv", "run", "--no-dev", "python", "-m", "aiwiki.service"]

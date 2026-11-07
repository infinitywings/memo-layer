# DeepSeek Harness with the rka-memory plugin. dsh needs Node >= 24.9, which
# this image provides without touching the host. Build from the repo root:
#   docker build -f docker/dsh/Dockerfile -t rka-memory-dsh .
FROM node:24-bookworm-slim

ARG DSH_VERSION=0.2.2-rc.1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*'

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /uvx /usr/local/bin/
RUN npm install -g --no-fund --no-audit "@deepseek-ai/dsh@${DSH_VERSION}"

ENV UV_PYTHON=3.12 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/rka-memory/.venv

# The workload's scripted agent runs `pytest -q` against fixture code inside
# dsh's sandbox. The tool lives outside /root, which dsh-docker mounts as tmpfs.
RUN UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin uv tool install pytest==9.2.0

WORKDIR /opt/rka-memory
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY rka_memory ./rka_memory
COPY plugins/dsh ./plugins/dsh
COPY scripts ./scripts
COPY docker/dsh ./docker/dsh
COPY workload ./workload
RUN uv sync --frozen --no-dev && chmod +x docker/dsh/*.sh

ENV PATH=/opt/rka-memory/.venv/bin:$PATH \
    DSH_HOME=/dsh-home \
    RKA_MEMORY_DIR=/rka-memory \
    RKA_MEMORY_CONFIG=/rka-memory/config.yaml

ENTRYPOINT ["/opt/rka-memory/docker/dsh/entrypoint.sh"]
CMD ["tui"]

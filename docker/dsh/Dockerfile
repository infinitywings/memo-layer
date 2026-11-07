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
RUN npm install -g --no-fund --no-audit "@deepseek-ai/dsh@${DSH_VERSION}" \
 && ln -s "$(ls /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/node-addon-system-linux-*/bin/landlock-run)" /usr/local/bin/landlock-run

# The agent's tools run as this user (docker/dsh/confine.sh), so a root-owned
# mode-700 ledger directory is out of their reach while dsh itself, and the
# plugin inside it, stay root. Workspaces the agent edits must be its own.
RUN useradd --create-home --shell /bin/sh agent \
 && mkdir -p /workspaces && chown agent:agent /workspaces

ENV UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

# The agent's Python: python, python3 and pytest on PATH, as on a developer's
# machine, outside /root, which dsh-docker mounts as tmpfs. The rka-memory
# venv stays off PATH and out of uv's settings: the plugin and the harness
# call it by absolute path, and an agent that can see it learns facts about
# this harness instead of its project.
RUN uv venv --python 3.12 /opt/agent-python \
 && uv pip install --python /opt/agent-python/bin/python pytest==9.2.0

WORKDIR /opt/rka-memory
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY rka_memory ./rka_memory
COPY plugins/dsh ./plugins/dsh
COPY scripts ./scripts
COPY docker/dsh ./docker/dsh
COPY workload ./workload
RUN uv sync --frozen --no-dev --python 3.12 && chmod +x docker/dsh/*.sh

ENV PATH=/opt/agent-python/bin:$PATH \
    DSH_HOME=/dsh-home \
    RKA_MEMORY_DIR=/rka-memory \
    RKA_MEMORY_CONFIG=/rka-memory/config.yaml \
    RKM_CONFINE_OVERLAY=/opt/rka-memory/docker/dsh/confine.yml

ENTRYPOINT ["/opt/rka-memory/docker/dsh/entrypoint.sh"]
CMD ["tui"]

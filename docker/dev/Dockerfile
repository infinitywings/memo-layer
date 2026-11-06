# Toolbox for developing and testing rka-memory inside the compose stack:
# Python 3.12 via uv, Node 24 for the dsh plugin tests, git and make.
# The repository is bind-mounted at /src; the virtualenv lives at /opt/venv
# so it never collides with the host's .venv.
FROM node:24-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates make \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system --add safe.directory '*'
COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /uvx /usr/local/bin/
ENV UV_PYTHON=3.12 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    RKA_MEMORY_VENV_BIN=/opt/venv/bin
RUN uv python install 3.12
WORKDIR /src

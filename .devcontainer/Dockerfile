FROM mcr.microsoft.com/devcontainers/base:noble

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Shared paths so the interpreter installed here (as root) is usable by vscode;
# --default adds `python`/`python3` shims alongside `python3.14`
ENV UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python \
    UV_PYTHON_BIN_DIR=/usr/local/bin
RUN uv python install 3.14 --default

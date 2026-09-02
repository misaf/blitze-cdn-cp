FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections \
    PATH=/opt/blitzecdn/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates certbot git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/blitzecdn
COPY . /opt/blitzecdn
# `--extra backup --extra cache --extra hardening --extra origins --extra
# resolver` attaches the optional capability distributions from packages/.
# They are discovered through their
# `blitzecdn.plugins` entry points, never imported by the control plane, so an
# image built without one of these flags is a working controller that simply
# does not offer that capability.
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python \
        --extra backup --extra cache --extra hardening --extra origins \
        --extra resolver \
    && .venv/bin/ansible-galaxy collection install \
        --requirements-file ansible/requirements.yml \
        --collections-path /usr/share/ansible/collections

# python:slim is root by default but already supplies Debian's unprivileged
# nobody:nogroup account (65534:65534). The application tree is immutable and
# Ansible gives only the state and backup bind mounts to this identity.
USER nobody:nogroup
ENV HOME=/opt/blitzecdn/.state \
    BLITZE_PROJECT_DIR=/opt/blitzecdn \
    BLITZE_CONFIG=/opt/blitzecdn/blitzecdn.toml

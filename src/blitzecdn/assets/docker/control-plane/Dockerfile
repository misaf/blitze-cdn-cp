# Match the main CI gate's Python minor version. Pin the multi-platform index
# so both published architectures use reviewed base images.
FROM python:3.14-slim-trixie@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

COPY --from=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /usr/local/bin/uv

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
# One application distribution carries all capability code and runtime resources.
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python \
    && .venv/bin/ansible-galaxy collection install \
        --requirements-file src/blitzecdn/assets/ansible/requirements.yml \
        --collections-path /usr/share/ansible/collections

# python:slim is root by default but already supplies Debian's unprivileged
# nobody:nogroup account (65534:65534). The application tree is immutable and
# Ansible gives only the state and backup bind mounts to this identity.
#
# Its passwd home is pointed at the state mount, the same place as HOME below:
# Ansible expands `~nobody` from passwd for a task delegated to the controller,
# and Debian's /nonexistent leaves it nowhere to put its temporary files.
RUN usermod --home /opt/blitzecdn/.state nobody
USER nobody:nogroup
ENV HOME=/opt/blitzecdn/.state \
    BLITZE_PROJECT_DIR=/opt/blitzecdn \
    BLITZE_CONFIG=/opt/blitzecdn/blitzecdn.toml

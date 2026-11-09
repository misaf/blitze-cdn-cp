# Match the main CI gate's Python minor version. Pin the multi-platform index
# so both published architectures use reviewed base images. Both stages name
# the same digest: the virtualenv built in the first links to its interpreter
# by path, so the second has to provide the identical Python.
#
# Build stage. Not every runtime dependency publishes Python 3.15 wheels yet
# (PyYAML does not), so the environment is built where a C toolchain exists
# and only the finished tree is copied into the runtime image.
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

# libyaml-dev gives PyYAML its C loader and dumper; Ansible parses every
# playbook and variables file through it.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        gcc libc6-dev libyaml-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/blitzecdn
COPY . /opt/blitzecdn
# One application distribution carries all capability code and runtime resources.
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python

# Runtime stage.
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

COPY --from=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections \
    PATH=/opt/blitzecdn/.venv/bin:$PATH

# libyaml-0-2 is the shared library PyYAML's C extension was linked against.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates certbot git libyaml-0-2 openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/blitzecdn
COPY --from=build /opt/blitzecdn /opt/blitzecdn
RUN .venv/bin/ansible-galaxy collection install \
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

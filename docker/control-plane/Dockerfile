FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ARG BLITZE_UID=999
ARG BLITZE_GID=999

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections \
    PATH=/opt/blitzecdn/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates certbot git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${BLITZE_GID}" blitzecdn \
    && useradd --uid "${BLITZE_UID}" --gid "${BLITZE_GID}" \
        --home-dir /var/lib/blitzecdn --create-home blitzecdn

WORKDIR /opt/blitzecdn
COPY . /opt/blitzecdn
RUN uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python \
    && .venv/bin/ansible-galaxy collection install \
        --requirements-file ansible/requirements.yml \
        --collections-path /usr/share/ansible/collections \
    && mkdir -p /var/backups/blitzecdn /opt/blitzecdn/.state \
    && chown -R blitzecdn:blitzecdn \
        /opt/blitzecdn /var/backups/blitzecdn /var/lib/blitzecdn

USER blitzecdn
ENV HOME=/var/lib/blitzecdn \
    BLITZE_PROJECT_DIR=/opt/blitzecdn \
    BLITZE_CONFIG=/opt/blitzecdn/blitzecdn.toml

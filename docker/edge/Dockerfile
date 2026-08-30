# BlitzeCDN edge runtime.
#
# Everything a BlitzeCDN edge needs to serve traffic, and nothing else. The host
# keeps Linux, SSH, Docker, the firewall and kernel tuning; this image keeps
# Nginx and its dynamic modules.
#
# Ubuntu 26.04 is deliberately the same platform the edges used to run natively.
# The four packages below are the exact set the host used to install, so the
# software serving traffic did not change when it moved into a container — only
# where it lives. That is what makes the migration reviewable: a behaviour
# difference between a native edge and a containerised one would have to come
# from the mounts or the compose file, never from a different Nginx.
#
# BlitzeCDN treats Nginx and its dynamic modules as one ABI-compatible unit.
# Ubuntu expresses that by pinning every libnginx-mod-* package to an
# nginx-abi-<version> virtual package, so apt resolving all four in one
# transaction is the guarantee. The probe at the end of this build is the proof:
# it loads all three modules and uses a directive from each, so an image that
# would fail `nginx -t` on a real edge fails here instead.
ARG UBUNTU_VERSION=26.04
FROM ubuntu:${UBUNTU_VERSION}

# Recorded into the image so `docker inspect` on an edge answers "which build is
# this" without consulting the control plane. Supplied by the release workflow.
ARG BLITZECDN_EDGE_VERSION=dev
ARG BLITZECDN_EDGE_REVISION=unknown

LABEL org.opencontainers.image.title="BlitzeCDN edge" \
      org.opencontainers.image.description="Nginx edge runtime with GeoIP2, Brotli, njs, HTTP/2 and HTTP/3." \
      org.opencontainers.image.source="https://github.com/misaf/blitze-cdn-cp" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.version="${BLITZECDN_EDGE_VERSION}" \
      org.opencontainers.image.revision="${BLITZECDN_EDGE_REVISION}"

# One transaction, so apt resolves the binary and all three modules against a
# single nginx-abi. Splitting them across RUN layers is exactly how an
# ABI-mismatched stack gets built without anyone noticing.
#
# ca-certificates is not optional: proxy_ssl_verify under SSL mode Full (strict)
# reads /etc/ssl/certs/ca-certificates.crt from inside this container.
# curl is here for the health check, which asks Nginx a question over HTTP
# rather than trusting that a process exists.
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install --no-install-recommends -y \
        nginx \
        libnginx-mod-http-geoip2 \
        libnginx-mod-http-brotli-filter \
        libnginx-mod-http-js \
        ca-certificates \
        curl \
        tzdata; \
    rm -rf /var/lib/apt/lists/*

# The distribution's own example site claims the default_server position, which
# on a BlitzeCDN edge belongs to the catch-all the control plane renders. The
# host's sites-enabled is mounted over this directory at run time, so the link
# would be masked anyway — removing it means the image alone is never a source
# of a second default_server.
RUN rm -f /etc/nginx/sites-enabled/default

# Build-time proof of the invariant this image exists to hold. Loading the three
# modules is not enough on its own: a module can load and still not register the
# directives BlitzeCDN renders, so the probe uses one from each.
COPY module-probe.conf /usr/share/blitzecdn/module-probe.conf
RUN set -eux; \
    nginx -v; \
    nginx -V 2>&1 | grep -q -- '--with-http_v3_module'; \
    nginx -V 2>&1 | grep -q -- '--build=Ubuntu'; \
    nginx -t -c /usr/share/blitzecdn/module-probe.conf

COPY healthcheck.sh /usr/local/bin/blitzecdn-healthcheck
RUN chmod 0755 /usr/local/bin/blitzecdn-healthcheck

# Deliberately no VOLUME declarations. Every directory that must survive the
# container is a host bind mount named in the Compose file, so an operator can
# see and back up the whole of an edge's state with `ls`, and a `docker compose
# down -v` cannot take a customer's TLS material or cache with it.

# start-period covers the seconds Nginx spends opening thirteen listeners and
# reading every site, and is what makes `docker compose up --wait` usable as the
# gate on a recreated edge. The interval is not shorter than this because the
# probe re-parses the whole configuration, which on an edge with hundreds of
# sites is real work to repeat.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/usr/local/bin/blitzecdn-healthcheck"]

STOPSIGNAL SIGQUIT

# Foreground, because the container *is* the process. `daemon off` rather than
# the package's init script: Docker restarts a dead container, and an Nginx that
# forks away leaves the engine supervising nothing.
CMD ["nginx", "-g", "daemon off;"]

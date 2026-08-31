# BlitzeCDN edge runtime.
#
# Start with the official, versioned Nginx image and add only the third-party
# modules and runtime files BlitzeCDN needs. GeoIP2 and Brotli are built with
# Nginx's own pkg-oss tooling against this exact base image, keeping the binary
# and dynamic modules ABI-compatible.
ARG NGINX_IMAGE=nginx:1.31.4-alpine
FROM ${NGINX_IMAGE} AS module-builder

ARG ENABLED_MODULES="geoip2 brotli"

SHELL ["/bin/ash", "-exo", "pipefail", "-c"]

# This is the module build flow maintained for the official Nginx image. The
# pkg-oss branch is derived from the version and package release recorded in
# the base image, so it cannot accidentally build against another Nginx ABI.
RUN test -n "${ENABLED_MODULES}" \
    && apk add --no-cache \
        abuild \
        alpine-sdk \
        coreutils \
        curl \
        git \
        libxml2-utils \
        linux-headers \
        openssl \
        openssl-dev \
        pcre2-dev \
        xz \
        zlib-dev \
    && printf '#!/bin/sh\nSETFATTR=true /usr/bin/abuild -F "$@"\n' > /usr/local/bin/abuild \
    && chmod 0755 /usr/local/bin/abuild \
    && git clone --depth 1 --branch "${NGINX_VERSION}-${PKG_RELEASE}" \
        https://github.com/nginx/pkg-oss.git /pkg-oss \
    && mkdir /tmp/packages \
    && for module in ${ENABLED_MODULES}; do \
        make -C /pkg-oss/alpine "abuild-module-${module}" \
            BASE_VERSION="${NGINX_VERSION}" NGINX_VERSION="${NGINX_VERSION}"; \
        apk add $(. "/pkg-oss/alpine/abuild-module-${module}/APKBUILD"; echo ${makedepends}); \
        make -C /pkg-oss/alpine "module-${module}" \
            BASE_VERSION="${NGINX_VERSION}" NGINX_VERSION="${NGINX_VERSION}"; \
        find /root/packages -type f -name '*.apk' -exec mv -v {} /tmp/packages/ \;; \
      done

FROM ${NGINX_IMAGE}

# Recorded into the image so `docker inspect` on an edge answers which build is
# running without consulting the control plane. Supplied by the release job.
ARG BLITZECDN_EDGE_VERSION=dev
ARG BLITZECDN_EDGE_REVISION=unknown

LABEL org.opencontainers.image.title="BlitzeCDN edge" \
      org.opencontainers.image.description="Nginx edge runtime with GeoIP2, Brotli, njs, HTTP/2 and HTTP/3." \
      org.opencontainers.image.source="https://github.com/misaf/blitze-cdn-cp" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.version="${BLITZECDN_EDGE_VERSION}" \
      org.opencontainers.image.revision="${BLITZECDN_EDGE_REVISION}"

# Install the two packages produced against this exact official base. njs ships
# in the official image; tzdata supports the configured edge timezone.
RUN --mount=type=bind,target=/tmp/packages,source=/tmp/packages,from=module-builder \
    apk add --no-cache --allow-untrusted \
        /tmp/packages/nginx-module-geoip2-${NGINX_VERSION}*.apk \
        /tmp/packages/nginx-module-brotli-${NGINX_VERSION}*.apk \
        tzdata

# BlitzeCDN owns the default server, not the example bundled with the image.
# Keep the official persistent log destinations and add Docker's stdout/stderr
# streams at the same configuration level. Managed sites declare their own
# access logs below this level and therefore add stdout in site.conf.j2 too.
RUN set -eux; \
    rm -f /etc/nginx/conf.d/default.conf; \
    sed -i '/^error_log .*notice;$/a\error_log  /dev/stderr notice;' /etc/nginx/nginx.conf; \
    sed -i '/^    access_log .*main;$/a\    access_log  /dev/stdout  main;' /etc/nginx/nginx.conf; \
    grep -q '^error_log  /dev/stderr notice;$' /etc/nginx/nginx.conf; \
    grep -q '^    access_log  /dev/stdout  main;$' /etc/nginx/nginx.conf

# Prove HTTP/3 and every dynamic module directive before publishing the image.
COPY module-probe.conf /usr/share/blitzecdn/module-probe.conf
RUN set -eux; \
    nginx -v; \
    nginx -V 2>&1 | grep -q -- '--with-http_v3_module'; \
    nginx -t -c /usr/share/blitzecdn/module-probe.conf

COPY healthcheck.sh /usr/local/bin/blitzecdn-healthcheck
RUN chmod 0755 /usr/local/bin/blitzecdn-healthcheck

# Persistent configuration, TLS, cache, logs and GeoIP data are explicit host
# bind mounts in Compose; the image intentionally declares no volumes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/usr/local/bin/blitzecdn-healthcheck"]

STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]

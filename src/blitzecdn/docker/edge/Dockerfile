# BlitzeCDN edge runtime.
#
# Start with the official, versioned Nginx image and add only the third-party
# modules and runtime files BlitzeCDN needs. Which modules those are is not
# written down here: it is an input, emitted by `blitzecdn edge image spec`
# from the capability distributions that declare them, so attaching a
# capability that needs a module extends this image without the Dockerfile
# learning the capability's name. It used to enumerate them, which made the
# image a third register of which capabilities exist — and the only one that
# kept naming a capability after its distribution had been detached.
#
# The modules named are built with Nginx's own pkg-oss tooling against this
# exact base image, keeping the binary and dynamic modules ABI-compatible.
ARG NGINX_IMAGE=nginx:1.31.4-alpine
FROM ${NGINX_IMAGE} AS module-builder

# The pkg-oss module names to build, space separated. Empty builds nothing,
# which is a valid image: everything BlitzeCDN needs from Nginx itself —
# HTTP/1.1, HTTP/2, HTTP/3, gzip, proxying, TLS — is compiled in, and a
# controller with no module-carrying capability installed loads none.
ARG ENABLED_MODULES=""

SHELL ["/bin/ash", "-exo", "pipefail", "-c"]

# This is the module build flow maintained for the official Nginx image. The
# pkg-oss branch is derived from the version and package release recorded in
# the base image, so it cannot accidentally build against another Nginx ABI.
#
# The output directory is created either way: the runtime stage bind-mounts it,
# and a mount source that does not exist fails the build for a reason that has
# nothing to do with modules.
RUN mkdir -p /tmp/packages \
    && if [ -n "${ENABLED_MODULES}" ]; then \
        apk add --no-cache \
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
        && for module in ${ENABLED_MODULES}; do \
            make -C /pkg-oss/alpine "abuild-module-${module}" \
                BASE_VERSION="${NGINX_VERSION}" NGINX_VERSION="${NGINX_VERSION}"; \
            apk add $(. "/pkg-oss/alpine/abuild-module-${module}/APKBUILD"; echo ${makedepends}); \
            make -C /pkg-oss/alpine "module-${module}" \
                BASE_VERSION="${NGINX_VERSION}" NGINX_VERSION="${NGINX_VERSION}"; \
            find /root/packages -type f -name '*.apk' -exec mv -v {} /tmp/packages/ \;; \
          done; \
      fi

FROM ${NGINX_IMAGE}

# The shared objects the image's own module list loads, space separated, and
# one directive per module for the build-time probe below. Both are emitted by
# `blitzecdn edge image spec` from the same declarations ENABLED_MODULES comes
# from, so the three cannot describe different sets.
#
# LOADED_MODULES is deliberately the wider list of the two module inputs: it
# names what the base image already ships as well as what was just built —
# njs is the standing case — because "which modules exist in this image" and
# "which modules this image had to compile" are different questions.
ARG LOADED_MODULES=""
ARG MODULE_PROBE_DIRECTIVES=""

# Recorded into the image so `docker inspect` on an edge answers which build is
# running without consulting the control plane. Supplied by the release job.
ARG BLITZECDN_EDGE_VERSION=dev
ARG BLITZECDN_EDGE_REVISION=unknown

LABEL org.opencontainers.image.title="BlitzeCDN edge" \
      org.opencontainers.image.description="Nginx edge runtime with HTTP/2, HTTP/3 and the dynamic modules the installed capabilities declare." \
      org.opencontainers.image.source="https://github.com/misaf/blitze-cdn-cp" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.version="${BLITZECDN_EDGE_VERSION}" \
      org.opencontainers.image.revision="${BLITZECDN_EDGE_REVISION}"

# Repeated rather than inherited: `SHELL` is per-stage, and the builder's
# declaration above does not reach here. Without it every pipeline in this
# stage reports only its *last* command's status — which for the module probe
# below means `nginx -V | grep` would pass on an nginx that failed to run at
# all, and the image would publish with the HTTP/3 assertion never evaluated.
# Each `RUN` still sets its own `-eux`; this is the part a `set` cannot state.
SHELL ["/bin/ash", "-eo", "pipefail", "-c"]

# Whatever the builder stage produced, against this exact official base. Each
# package's filename has to carry the base image's Nginx version: pkg-oss
# builds them that way by construction, so a package that does not is a build
# that resolved a different ABI and must not be installed silently.
#
# tzdata supports the configured edge timezone; curl is a runtime dependency,
# not a convenience — the Compose healthcheck execs it, so dropping it turns
# every edge unhealthy.
RUN --mount=type=bind,target=/tmp/packages,source=/tmp/packages,from=module-builder \
    set -eux; \
    apk add --no-cache curl tzdata; \
    for package in /tmp/packages/*.apk; do \
        [ -e "${package}" ] || break; \
        case "$(basename "${package}")" in \
            *"${NGINX_VERSION}"*) ;; \
            *) echo "module package ${package} was not built for nginx ${NGINX_VERSION}" >&2; exit 1;; \
        esac; \
    done; \
    if [ -n "$(ls -A /tmp/packages 2>/dev/null)" ]; then \
        apk add --no-cache --allow-untrusted /tmp/packages/*.apk; \
    fi

# BlitzeCDN owns the default server, not the example bundled with the image.
# Keep the official persistent log destinations and add Docker's stdout/stderr
# streams at the same configuration level. Managed sites declare their own
# access logs below this level and therefore add stdout in site.conf.j2 too.
#
# The module list written here is the image's own, and it is the *superset*: an
# edge mounts the control plane's narrower list over this path, so the file
# matters at build time — the probe below loads exactly it — and as the answer
# for anything running this image without a controller.
RUN set -eux; \
    : > /etc/nginx/modules.conf; \
    for object in ${LOADED_MODULES}; do \
        test -f "/usr/lib/nginx/modules/${object}"; \
        echo "load_module /usr/lib/nginx/modules/${object};" >> /etc/nginx/modules.conf; \
    done; \
    rm -f /etc/nginx/conf.d/default.conf; \
    sed -i '1i\include /etc/nginx/modules.conf;' /etc/nginx/nginx.conf; \
    sed -i '/^error_log .*notice;$/a\error_log  /dev/stderr notice;' /etc/nginx/nginx.conf; \
    sed -i '/^    access_log .*main;$/a\    access_log  /dev/stdout  main;' /etc/nginx/nginx.conf; \
    sed -i '/include \/etc\/nginx\/conf.d\/\*.conf;/a\    include /etc/nginx/sites-enabled/*;' /etc/nginx/nginx.conf; \
    grep -q '^include /etc/nginx/modules.conf;$' /etc/nginx/nginx.conf; \
    grep -q '^error_log  /dev/stderr notice;$' /etc/nginx/nginx.conf; \
    grep -q '^    access_log  /dev/stdout  main;$' /etc/nginx/nginx.conf; \
    grep -q '^    include /etc/nginx/sites-enabled/\*;$' /etc/nginx/nginx.conf

# Prove HTTP/3 and every dynamic module directive before publishing the image.
# The probe file names no module: it includes the generated list, and the
# directives it evaluates are the ones the capabilities declared alongside the
# modules they asked for.
COPY module-probe.conf /usr/share/blitzecdn/module-probe.conf
RUN set -eux; \
    printf '%s\n' "${MODULE_PROBE_DIRECTIVES}" \
        > /usr/share/blitzecdn/module-probe-directives.conf; \
    nginx -v; \
    nginx -V 2>&1 | grep -q -- '--with-http_v3_module'; \
    nginx -t -c /usr/share/blitzecdn/module-probe.conf

# Persistent configuration, TLS, cache, logs and capability data are explicit
# host bind mounts in Compose; the image intentionally declares no volumes.
#
# No HEALTHCHECK here, deliberately: the status endpoint's address, port and
# path are rendered by blitzecdn_nginx, so the probe reading them lives in the
# Compose file rendered from the same variables rather than as a baked-in URL.
STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]

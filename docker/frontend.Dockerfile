# Phase 8A - Static container for the zero-build operator dashboard.
# Phase 10B - single web entry point: this nginx also reverse-proxies /api/ to
#   the backend over the private Docker network (see docker/nginx.conf); the
#   dashboard talks to the SAME ORIGIN, so no backend port is public and no CORS
#   is needed. The dashboard file itself is served unchanged.
#
# Base image (security): pinned to a MAINTAINED, patched Nginx on Alpine 3.24
# (the previous 1.27-alpine resolved to Alpine 3.21.3, which was behind on
# security patches -> 41 HIGH + 2 CRITICAL fixable OS CVEs flagged by Trivy).
# 1.30 is the current stable Nginx line; alpine3.24 is the current Alpine base.
# `apk upgrade` then pulls any package fixes published since the base was built,
# so the image carries no fixable HIGH/CRITICAL OS CVEs. This complements (does
# not replace) the maintained base choice.
FROM nginx:1.30.5-alpine3.24

# Pull the latest patched Alpine packages for this release (defense in depth on
# top of the already-current base). --no-cache keeps the layer clean.
RUN apk --no-cache upgrade

# Harden the default server a little; serve the dashboard as the site root.
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/index.html /usr/share/nginx/html/index.html

EXPOSE 80

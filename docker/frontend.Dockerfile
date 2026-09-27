# Phase 8A - Static container for the zero-build operator dashboard.
#
# The dashboard (frontend/index.html) is a single self-contained file that talks
# to the backend from the browser (client-side fetch). It is NOT redesigned here:
# we just serve the exact file over nginx. The browser reaches the API on the
# published backend port; the backend already CORS-allow-lists localhost:8080.
FROM nginx:1.27-alpine

# Harden the default server a little; serve the dashboard as the site root.
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/index.html /usr/share/nginx/html/index.html

EXPOSE 80

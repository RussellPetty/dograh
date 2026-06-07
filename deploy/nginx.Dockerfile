# Tiny nginx image for the Coolify single-domain reverse proxy.
# Build context is the repo root (so the conf path resolves).
FROM nginx:alpine
COPY deploy/nginx.coolify.conf /etc/nginx/conf.d/default.conf
EXPOSE 80

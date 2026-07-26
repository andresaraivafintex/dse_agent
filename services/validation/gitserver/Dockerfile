# WSE-E4-T10 — git SMART HTTP server for the preview manifests repo.
# Why it exists: Argo CD resolves revisions through go-git (ls-remote), which
# does NOT speak the dumb HTTP protocol (static files) — only smart HTTP. This
# container serves `git-http-backend` (git's official CGI) behind
# nginx+fcgiwrap. Pinned base (P7). Read-only: upload-pack (fetch) only; the
# host pushes straight into the filesystem (bind mount) — receive-pack stays
# disabled.
FROM alpine:3.20

RUN apk add --no-cache git git-daemon fcgiwrap spawn-fcgi nginx

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 80

CMD ["sh", "-c", "spawn-fcgi -s /var/run/fcgiwrap.sock -M 666 /usr/bin/fcgiwrap && exec nginx -g 'daemon off;'"]

# WSE-E4-T10 — git SMART HTTP server para o repo de manifests de preview.
# Por que existe: o Argo CD resolve revisões via go-git (ls-remote), que NÃO
# fala o protocolo dumb HTTP (arquivo estático) — só smart HTTP. Este container
# serve `git-http-backend` (CGI oficial do git) atrás de nginx+fcgiwrap.
# Base pinada (P7). Read-only: só upload-pack (fetch); push é feito pelo host
# direto no filesystem (bind mount) — receive-pack fica desabilitado.
FROM alpine:3.20

RUN apk add --no-cache git git-daemon fcgiwrap spawn-fcgi nginx

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 80

CMD ["sh", "-c", "spawn-fcgi -s /var/run/fcgiwrap.sock -M 666 /usr/bin/fcgiwrap && exec nginx -g 'daemon off;'"]

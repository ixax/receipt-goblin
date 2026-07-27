# Config baked in at build time (this repo's convention), not bind-mounted.
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/nginx.conf
# Landing page listing every service link (see nginx.conf's `listen 80`
# block). Paths are hardcoded in the template, but published ports are only
# known at `docker compose up` time (.env/shell) - not at image build time -
# so the template is rendered into the real index.html by
# docker-entrypoint.d/50-render-index-html.sh on every container start
# instead, via the base nginx image's own /docker-entrypoint.d/ extension
# point (see that script for why this isn't a custom ENTRYPOINT).
COPY index.html.template /etc/nginx/html-template/index.html.template
COPY docker-entrypoint.d/50-render-index-html.sh /docker-entrypoint.d/50-render-index-html.sh
# Belt-and-suspenders alongside the executable bit already set on disk (git
# doesn't always preserve it depending on how the repo was checked out).
RUN chmod +x /docker-entrypoint.d/50-render-index-html.sh

EXPOSE 80 8000 4000 3000 3001 8001 8123 9000

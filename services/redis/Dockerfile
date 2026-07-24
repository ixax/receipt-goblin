# Prod image - redis.conf baked in at build time (this repo's convention -
# see AGENTS.md / feedback memory "no config volume mounts"), not
# bind-mounted. A tuning change means rebuilding this image
# (`docker compose build redis`), not editing a mounted file.
FROM redis:7-alpine

COPY redis.conf /usr/local/etc/redis/redis.conf

CMD ["redis-server", "/usr/local/etc/redis/redis.conf"]

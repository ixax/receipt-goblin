# Prod image - redis.conf baked in at build time (this repo's convention),
# not bind-mounted. A tuning change means rebuilding this image.
FROM redis:7-alpine

COPY redis.conf /usr/local/etc/redis/redis.conf

CMD ["redis-server", "/usr/local/etc/redis/redis.conf"]

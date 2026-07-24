# Stock image, no config to bake - only the startup script (below), which
# replaces what used to be an inline compose `entrypoint`/`command`.
FROM minio/minio:latest

COPY --chmod=0755 entrypoint.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

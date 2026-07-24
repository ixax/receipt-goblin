# Stock image, no config to bake - the only per-repo customization is the
# CLI flags (below), previously a compose-level `command:` override, now
# baked as this image's own CMD so docker-compose.yml needs none.
FROM prom/node-exporter:v1.8.2

CMD ["--path.procfs=/host/proc", \
     "--path.sysfs=/host/sys", \
     "--path.rootfs=/host/root", \
     "--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($|/)"]

# Stock image - only customization is the CLI flags below, baked as this
# image's own CMD so docker-compose.yml needs no `command:` override.
FROM prom/node-exporter:v1.8.2

CMD ["--path.procfs=/host/proc", \
     "--path.sysfs=/host/sys", \
     "--path.rootfs=/host/root", \
     "--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($|/)"]

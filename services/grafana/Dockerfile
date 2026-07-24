# Prod image - config/provisioning/dashboards baked in at build time (this
# repo's convention), not bind-mounted. docker-compose.dev.yml still
# bind-mounts all of this back over the image for live local editing.
FROM grafana/grafana:13.1.0

COPY --chmod=0755 docker-entrypoint.sh /docker-entrypoint-custom.sh
COPY config.yml /etc/grafana/config.yml
COPY provisioning/datasources /etc/grafana/provisioning-templates/datasources
COPY provisioning/dashboards /etc/grafana/provisioning/dashboards
COPY provisioning/alerting /etc/grafana/provisioning/alerting
# Not /var/lib/grafana/dashboards - that tree is shadowed at runtime by the
# grafana-data named volume, which would hide anything baked in under it.
COPY dashboards /etc/grafana/dashboards
# Health-folder dashboards need a separate top-level path since two
# provisioning providers can't share one path.
COPY dashboards-health /etc/grafana/dashboards-health

ENTRYPOINT ["/bin/sh", "/docker-entrypoint-custom.sh"]

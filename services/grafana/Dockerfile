# Prod image - config/provisioning/dashboards baked in at build time (this
# repo's convention - see AGENTS.md / feedback memory "no config volume
# mounts"), not bind-mounted. A dashboard or provisioning change means
# rebuilding this image (`docker compose build grafana`), not editing a
# mounted file - docker-compose.dev.yml still bind-mounts all of this back
# over the image for live local editing.
FROM grafana/grafana:13.1.0

COPY --chmod=0755 docker-entrypoint.sh /docker-entrypoint-custom.sh
COPY config.yml /etc/grafana/config.yml
COPY provisioning/datasources /etc/grafana/provisioning-templates/datasources
COPY provisioning/dashboards /etc/grafana/provisioning/dashboards
COPY provisioning/alerting /etc/grafana/provisioning/alerting
# Not /var/lib/grafana/dashboards - that whole tree is shadowed at runtime by
# the grafana-data named volume (docker-compose.yml), which would hide
# anything baked in under it. See provisioning/dashboards/dashboard.yml's
# `path:` comment.
COPY dashboards /etc/grafana/dashboards
# Health-folder dashboards live under a separate top-level path, not nested
# under dashboards/ above - see provisioning/dashboards/dashboard.yml's
# `health` provider comment for why (two providers can't share a path).
COPY dashboards-health /etc/grafana/dashboards-health

ENTRYPOINT ["/bin/sh", "/docker-entrypoint-custom.sh"]

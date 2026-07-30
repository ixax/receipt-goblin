FROM nginx/nginx-prometheus-exporter:1.3.0 as nginx-src

FROM alpine:3.19
RUN apk add --no-cache ca-certificates curl

COPY --from=nginx-src /usr/bin/nginx-prometheus-exporter /usr/bin/nginx-prometheus-exporter

EXPOSE 9113

ENTRYPOINT ["/usr/bin/nginx-prometheus-exporter"]

FROM oliver006/redis_exporter:v1.66.0 as redis-src

FROM alpine:3.19
RUN apk add --no-cache ca-certificates curl

COPY --from=redis-src /redis_exporter /redis_exporter

EXPOSE 9121

CMD ["/redis_exporter"]

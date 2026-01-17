FROM alpine:3.20
RUN apk add --no-cache bash ca-certificates curl jq inotify-tools tzdata
WORKDIR /app
RUN mkdir -p /inbox
COPY watcher.sh /app/watcher.sh
RUN chmod +x /app/watcher.sh
ENTRYPOINT ["/app/watcher.sh"]

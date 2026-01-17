FROM alpine:3.20
RUN apk add --no-cache bash ca-certificates curl jq coreutils
WORKDIR /app
COPY watcher.sh /app/watcher.sh
RUN chmod +x /app/watcher.sh
ENV TZ=${TZ:-UTC}
ENTRYPOINT ["/app/watcher.sh"]
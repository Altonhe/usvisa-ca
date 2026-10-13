FROM golang:1.25-bookworm AS build

WORKDIR /src
COPY . .
RUN go mod tidy
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/openvpn-provider .

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      iproute2 \
      netcat-openbsd \
      openvpn \
      tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/openvpn-provider /usr/local/bin/openvpn-provider

EXPOSE 1080 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/openvpn-provider"]

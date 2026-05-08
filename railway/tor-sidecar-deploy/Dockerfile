# Sthrip Tor sidecar — Hidden Service v3 + SOCKS5 proxy.
#
# Deploys as a separate Railway service in the `sthrip` project.
# Mount a persistent volume on `/var/lib/tor/sthrip-hsv3` so the
# v3 hidden-service keys (and therefore the .onion address) survive
# restarts. Without that volume, every restart yields a new .onion.
#
# Inbound:  HiddenServicePort 80 -> sthrip-api.railway.internal:8000
# Outbound: SocksPort 9050 (consumed by the API container only when
#           STHRIP_ONION_ENABLED=true and target endswith .onion;
#           per Lead Q4, never all-traffic).
#
# This image runs as the unprivileged `tor` user. ControlPort is
# disabled so a compromised sidecar cannot be remotely steered.

FROM alpine:3.19

# tini gives us proper signal handling so SIGTERM cleanly stops tor
RUN apk add --no-cache tor tini ca-certificates \
 && mkdir -p /var/lib/tor/sthrip-hsv3 \
 && chown -R tor:tor /var/lib/tor /etc/tor \
 && chmod 700 /var/lib/tor/sthrip-hsv3

COPY torrc /etc/tor/torrc
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh \
 && chown root:root /etc/tor/torrc /entrypoint.sh \
 && chmod 0644 /etc/tor/torrc

USER tor
EXPOSE 9050

ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]

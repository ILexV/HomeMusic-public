#!/bin/bash
# HomeMusic gateway on the VPS (run as root, idempotent):
#   - user homemusic-tunnel that may only open reverse listens 127.0.0.1:4533 (Navidrome) and 127.0.0.1:8080 (Hub)
#   - watchdog that kills stale tunnel sessions (so the ports are freed for a reconnect)
#   - nft guard: 4533/8080 reachable only from loopback
#   - Caddy sites: https://<ip>:8444 -> Navidrome, https://<ip>:8445 -> Hub (Let's Encrypt certificate for the IP)
# Usage: setup-vps.sh <public-ip> "<tunnel public key line>"
# Other services on the VPS are not touched. Every changed file is backed up first.
set -euo pipefail

PUBLIC_IP=${1:?public ip}
PUBKEY=${2:?tunnel public key}
MUSIC_PORT=${MUSIC_PORT:-8444}
HUB_PORT=${HUB_PORT:-8445}
USER_NAME=homemusic-tunnel
USER_HOME=/var/lib/$USER_NAME
BACKUP=/root/homemusic-gateway-backup/$(date +%Y%m%d-%H%M%S)
CADDYFILE=/etc/caddy/Caddyfile

mkdir -p "$BACKUP"
backup() { for f in "$@"; do [ -e "$f" ] && cp -a --parents "$f" "$BACKUP"/ || true; done; }
backup "$CADDYFILE" /etc/ssh/sshd_config.d/61-homemusic-tunnel.conf "$USER_HOME/.ssh/authorized_keys" \
  /usr/local/sbin/homemusic-tunnel-watchdog /etc/systemd/system/homemusic-tunnel-watchdog.service \
  /etc/homemusic/guard.nft /etc/systemd/system/homemusic-guard-firewall.service

# 1. Tunnel user
if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$USER_HOME" --create-home --shell /usr/sbin/nologin \
    --comment "Reverse SSH tunnel for HomeMusic" "$USER_NAME"
fi
passwd -l "$USER_NAME" >/dev/null
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$USER_HOME/.ssh"
echo "restrict,port-forwarding,permitlisten=\"127.0.0.1:4533\",permitlisten=\"127.0.0.1:8080\",command=\"/usr/sbin/nologin\" $PUBKEY" \
  > "$USER_HOME/.ssh/authorized_keys"
chown "$USER_NAME:$USER_NAME" "$USER_HOME/.ssh/authorized_keys"
chmod 600 "$USER_HOME/.ssh/authorized_keys"

# 2. sshd restrictions for the tunnel user
cat > /etc/ssh/sshd_config.d/61-homemusic-tunnel.conf <<'EOF'
# HomeMusic reverse SSH tunnel (PC -> VPS): only -R 127.0.0.1:4533 and 127.0.0.1:8080, no shell or other forwards.
# Rollback: delete this file and run systemctl reload ssh.
Match User homemusic-tunnel
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:4533 127.0.0.1:8080
    PermitOpen none
    GatewayPorts no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    PermitUserRC no
    ForceCommand /usr/sbin/nologin
    ClientAliveInterval 15
    ClientAliveCountMax 3
EOF
sshd -t
systemctl reload ssh

# 3. Watchdog for stale sessions
cat > /usr/local/sbin/homemusic-tunnel-watchdog <<'EOF'
#!/bin/bash
# Kills reverse-tunnel sessions of homemusic-tunnel that got no data or ACK from the client for LIMIT_MS.
# A healthy client sends ServerAlive every 15 s. A stale session keeps 127.0.0.1:4533/8080 busy,
# and the client could not reconnect until sshd notices (requests through Caddy keep the session "active").
set -u
USER_NAME=homemusic-tunnel
LIMIT_MS=${LIMIT_MS:-45000}
INTERVAL=${INTERVAL:-10}
UID_T=$(id -u "$USER_NAME") || exit 1

check() {
  ss -tniHp state established '( sport = :22 )' 2>/dev/null | awk '
    /^[0-9]/ {
      peer = $4; pids = ""; s = $0
      while (match(s, /pid=[0-9]+/)) { pids = pids " " substr(s, RSTART + 4, RLENGTH - 4); s = substr(s, RSTART + RLENGTH) }
      next
    }
    /^[ \t]/ {
      rcv = -1; ack = -1; n = split($0, a, /[ \t]+/)
      for (i = 1; i <= n; i++) {
        if (a[i] ~ /^lastrcv:/) rcv = substr(a[i], 9) + 0
        if (a[i] ~ /^lastack:/) ack = substr(a[i], 9) + 0
      }
      idle = rcv
      if (ack >= 0 && (idle < 0 || ack < idle)) idle = ack
      if (pids != "" && idle >= 0) print peer, idle, pids
    }' | while read -r peer idle pids; do
      for pid in $pids; do
        [ "$(stat -c %u "/proc/$pid" 2>/dev/null)" = "$UID_T" ] || continue
        if [ "$idle" -gt "$LIMIT_MS" ]; then
          echo "stale tunnel session pid=$pid peer=$peer: no reply for ${idle} ms, terminating"
          kill -TERM "$pid" 2>/dev/null
        fi
      done
    done
}

while true; do
  check
  sleep "$INTERVAL"
done
EOF
chmod 755 /usr/local/sbin/homemusic-tunnel-watchdog

cat > /etc/systemd/system/homemusic-tunnel-watchdog.service <<'EOF'
[Unit]
Description=Watchdog: kill stale reverse SSH tunnel sessions of homemusic-tunnel
After=ssh.service

[Service]
ExecStart=/usr/local/sbin/homemusic-tunnel-watchdog
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

# 4. nft guard: tunnel ports only from loopback (own table, Docker/VPN rules untouched)
install -d /etc/homemusic
cat > /etc/homemusic/guard.nft <<'EOF'
#!/usr/sbin/nft -f
# HomeMusic tunnel ports (4533 Navidrome, 8080 Hub) are reachable only from loopback.
# Rollback: nft delete table inet homemusic_guard
table inet homemusic_guard
delete table inet homemusic_guard
table inet homemusic_guard {
	chain input {
		type filter hook input priority filter - 10; policy accept;
		iifname != "lo" tcp dport { 4533, 8080 } counter drop
		iifname != "lo" udp dport { 4533, 8080 } counter drop
	}
}
EOF
cat > /etc/systemd/system/homemusic-guard-firewall.service <<'EOF'
[Unit]
Description=nftables guard: HomeMusic tunnel ports 4533/8080 only from loopback
Wants=network-pre.target
Before=network-pre.target ssh.service caddy.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/homemusic/guard.nft
ExecStop=/usr/sbin/nft delete table inet homemusic_guard

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now homemusic-guard-firewall.service homemusic-tunnel-watchdog.service
systemctl restart homemusic-guard-firewall.service homemusic-tunnel-watchdog.service

# 5. Caddy sites (managed block between markers, replaced on every run)
BLOCK=$(cat <<EOF
# >>> homemusic (managed by HomeMusic gateway/vps/setup-vps.sh; edit there)
# Navidrome (Subsonic API, players: Tempus) and the Hub (PWA) arrive over the homemusic-tunnel reverse SSH tunnel.
${PUBLIC_IP}:${MUSIC_PORT} {
	encode zstd gzip
	reverse_proxy 127.0.0.1:4533
}

${PUBLIC_IP}:${HUB_PORT} {
	encode zstd gzip
	# 500 MiB of files + 1 MiB multipart framing; Hub enforces smaller route/config limits.
	request_body {
		max_size 525336576
	}
	# SSE (/api/events) is flushed immediately; upload bodies remain streamed.
	reverse_proxy 127.0.0.1:8080 {
		flush_interval -1
	}
}
# <<< homemusic
EOF
)
TMP=$(mktemp)
awk '/^# >>> homemusic/{skip=1} !skip{print} /^# <<< homemusic/{skip=0}' "$CADDYFILE" | sed -e :a -e '/^\n*$/{$d;N;ba' -e '}' > "$TMP"
printf '\n%s\n' "$BLOCK" >> "$TMP"
caddy fmt --overwrite "$TMP" >/dev/null 2>&1 || true
caddy validate --config "$TMP" --adapter caddyfile
install -m 644 -o root -g root "$TMP" "$CADDYFILE"
rm -f "$TMP"
systemctl reload caddy

echo "OK. Backup of changed files: $BACKUP"
echo "Navidrome: https://${PUBLIC_IP}:${MUSIC_PORT}   Hub: https://${PUBLIC_IP}:${HUB_PORT}"

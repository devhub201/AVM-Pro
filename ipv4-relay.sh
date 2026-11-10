#!/usr/bin/env bash
# AxionHost IPv4 relay: give an IPv6-only VPS host a public IPv4 (inbound ports + outbound internet) through a small IPv4 VPS using WireGuard.
#   RELAY  (small IPv4 VPS):   bash ipv4-relay.sh relay
#   HOST   (IPv6-only, LXD):   bash ipv4-relay.sh host <RELAY_ENDPOINT> <RELAY_PUBKEY>
#   RELAY  (after host step):  bash ipv4-relay.sh addpeer <HOST_PUBKEY>
#   test:                      bash ipv4-relay.sh status
# Env: PORT_START/PORT_END (default 20000-50000 = the range the AVM bot hands out), WG_PORT (51820), LXD_BRIDGE (lxdbr0). DRYRUN=1 prints instead of applying.
set -u
MODE="${1:-}"; PORT_START="${PORT_START:-20000}"; PORT_END="${PORT_END:-50000}"; WG_PORT="${WG_PORT:-51820}"; BR="${LXD_BRIDGE:-lxdbr0}"
RELAY_WG=10.77.0.1; HOST_WG=10.77.0.2; CONF=/etc/wireguard/wg0.conf; DRY="${DRYRUN:-0}"
ok(){ printf ' \033[32m✔\033[0m %s\n' "$*"; }; bad(){ printf ' \033[31m✘\033[0m %s\n' "$*"; }; say(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
run(){ if [ "$DRY" = 1 ]; then echo "  [dry] $*"; else "$@"; fi; }
[ "$DRY" = 1 ] || [ "$(id -u)" = 0 ] || { bad "root se chalao (sudo -i)"; exit 1; }
install_wg(){ command -v wg >/dev/null && return 0; export DEBIAN_FRONTEND=noninteractive; run apt-get update -qq >/dev/null 2>&1; run apt-get install -y -qq wireguard wireguard-tools iptables iproute2 curl >/dev/null 2>&1; command -v wg >/dev/null || [ "$DRY" = 1 ] || { bad "wireguard install nahi hua (apt-get install wireguard)"; exit 1; }; ok "wireguard"; }
keys(){ [ -f /etc/wireguard/priv ] || { mkdir -p /etc/wireguard; umask 077; wg genkey > /etc/wireguard/priv; }; wg pubkey < /etc/wireguard/priv > /etc/wireguard/pub; }
up(){ if [ "$DRY" = 1 ]; then return; fi; sysctl -w net.ipv4.ip_forward=1 >/dev/null; echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-axion-relay.conf
  if [ -d /run/systemd/system ]; then systemctl enable wg-quick@wg0 >/dev/null 2>&1; systemctl restart wg-quick@wg0; else wg-quick down wg0 2>/dev/null; wg-quick up wg0; fi; }
ufw_open(){ command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active" && { run ufw allow $WG_PORT/udp >/dev/null; run ufw allow $PORT_START:$PORT_END/tcp >/dev/null; run ufw allow $PORT_START:$PORT_END/udp >/dev/null; run sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw; run ufw reload >/dev/null; ok "ufw: ports khole"; }; }

case "$MODE" in
relay)
  say "RELAY setup"; install_wg; [ "$DRY" = 1 ] || keys
  WAN=$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}'); WAN=${WAN:-eth0}
  V4=$(curl -4 -s --max-time 6 ifconfig.me 2>/dev/null); V6=$(curl -6 -s --max-time 6 ifconfig.me 2>/dev/null)
  PRIV=$( [ "$DRY" = 1 ] && echo PRIVKEY || cat /etc/wireguard/priv ); PUB=$( [ "$DRY" = 1 ] && echo PUBKEY || cat /etc/wireguard/pub )
  EXISTING=""; [ -f "$CONF" ] && EXISTING=$(awk '/^\[Peer\]/{p=1} p' "$CONF")
  CONTENT="[Interface]
Address = $RELAY_WG/24
ListenPort = $WG_PORT
PrivateKey = $PRIV
MTU = 1280
PostUp = iptables -t nat -A PREROUTING -i $WAN -p tcp --dport $PORT_START:$PORT_END -j DNAT --to-destination $HOST_WG; iptables -t nat -A PREROUTING -i $WAN -p udp --dport $PORT_START:$PORT_END -j DNAT --to-destination $HOST_WG; iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE; iptables -t nat -A POSTROUTING -s 10.77.0.0/24 -o $WAN -j MASQUERADE; iptables -I FORWARD -i $WAN -o wg0 -j ACCEPT; iptables -I FORWARD -i wg0 -o $WAN -j ACCEPT
PostDown = iptables -t nat -D PREROUTING -i $WAN -p tcp --dport $PORT_START:$PORT_END -j DNAT --to-destination $HOST_WG; iptables -t nat -D PREROUTING -i $WAN -p udp --dport $PORT_START:$PORT_END -j DNAT --to-destination $HOST_WG; iptables -t nat -D POSTROUTING -o wg0 -j MASQUERADE; iptables -t nat -D POSTROUTING -s 10.77.0.0/24 -o $WAN -j MASQUERADE; iptables -D FORWARD -i $WAN -o wg0 -j ACCEPT; iptables -D FORWARD -i wg0 -o $WAN -j ACCEPT
$EXISTING"
  if [ "$DRY" = 1 ]; then echo "$CONTENT"; else umask 077; echo "$CONTENT" > $CONF; up; fi; ufw_open
  say "Ab HOST (IPv6 wali VPS) par yeh chalao:"
  EP=${V6:-$V4}; [ -n "$V6" ] && ok "Relay ke paas IPv6 hai - best (seedha connect)" || echo " (Relay IPv6 nahi - host NAT64 se IPv4 tak pahunchega, thoda unreliable ho sakta hai)"
  echo "  bash ipv4-relay.sh host $EP $PUB"
  echo; echo " Phir host ka public key yahan (relay par) daalna:  bash ipv4-relay.sh addpeer <HOST_PUBKEY>"
  echo " Relay IPv4 (bot config 'server_ip' mein yahi daalna): ${V4:-?}";;
addpeer)
  K="${2:-}"; [ ${#K} -ge 40 ] || { bad "Usage: bash ipv4-relay.sh addpeer <HOST_PUBKEY>"; exit 1; }
  if [ "$DRY" = 1 ]; then echo "  [dry] wg set wg0 peer $K allowed-ips $HOST_WG/32"; else
    wg set wg0 peer "$K" allowed-ips $HOST_WG/32 persistent-keepalive 25 && ok "peer joda"
    grep -q "$K" $CONF || printf '\n[Peer]\nPublicKey = %s\nAllowedIPs = %s/32\nPersistentKeepalive = 25\n' "$K" "$HOST_WG" >> $CONF; sleep 4
    wg show wg0 latest-handshakes | grep -q "$K" && ok "host connect ho gaya" || echo " handshake abhi nahi aaya - host par 'bash ipv4-relay.sh status' chalao"; fi;;
host)
  EP="${2:-}"; RK="${3:-}"; [ -n "$EP" ] && [ ${#RK} -ge 40 ] || { bad "Usage: bash ipv4-relay.sh host <RELAY_ENDPOINT> <RELAY_PUBKEY>"; exit 1; }
  say "HOST setup"; install_wg; [ "$DRY" = 1 ] || keys
  case "$EP" in *:*) EPA="[$EP]";; *) # IPv4 only relay -> NAT64 (host has no IPv4)
    IFS=. read -r a b c d <<<"$EP"; EPA=$(printf '[2a00:1098:2c::%x%02x:%x%02x]' $a $b $c $d); echo " IPv4 endpoint -> NAT64: $EPA";; esac
  NET=$(lxc network get "$BR" ipv4.address 2>/dev/null); [ -n "$NET" ] || { bad "LXD bridge '$BR' nahi mila (LXD installed? LXD_BRIDGE=... set karo)"; [ "$DRY" = 1 ] || exit 1; NET=10.0.0.1/24; }
  SUB=$(python3 -c "import ipaddress,sys;print(ipaddress.ip_interface(sys.argv[1]).network)" "$NET")
  PRIV=$( [ "$DRY" = 1 ] && echo PRIVKEY || cat /etc/wireguard/priv ); PUB=$( [ "$DRY" = 1 ] && echo PUBKEY || cat /etc/wireguard/pub )
  CONTENT="[Interface]
Address = $HOST_WG/24
PrivateKey = $PRIV
Table = off
MTU = 1280
PostUp = ip rule add from $SUB table 100 priority 100; ip route add default dev wg0 table 100; iptables -t nat -A POSTROUTING -o wg0 -j MASQUERADE; iptables -I INPUT -i wg0 -j ACCEPT; iptables -I FORWARD -i $BR -o wg0 -j ACCEPT; iptables -I FORWARD -i wg0 -o $BR -j ACCEPT
PostDown = ip rule del from $SUB table 100 priority 100; ip route del default dev wg0 table 100; iptables -t nat -D POSTROUTING -o wg0 -j MASQUERADE; iptables -D INPUT -i wg0 -j ACCEPT; iptables -D FORWARD -i $BR -o wg0 -j ACCEPT; iptables -D FORWARD -i wg0 -o $BR -j ACCEPT

[Peer]
PublicKey = $RK
Endpoint = $EPA:$WG_PORT
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25"
  if [ "$DRY" = 1 ]; then echo "$CONTENT"; else umask 077; echo "$CONTENT" > $CONF; up; fi
  say "Ab RELAY (IPv4 VPS) par yeh chalao:"; echo "  bash ipv4-relay.sh addpeer $PUB"; echo; echo " Phir yahan: bash ipv4-relay.sh status";;
status)
  say "STATUS"; wg show wg0 2>&1 | grep -E "endpoint|handshake|transfer|peer" | sed 's/^/ /'
  if ping -c1 -W3 $RELAY_WG >/dev/null 2>&1; then ok "relay ping ($RELAY_WG) OK"; elif ping -c1 -W3 $HOST_WG >/dev/null 2>&1; then ok "host ping ($HOST_WG) OK"; else bad "tunnel ping fail (handshake? firewall UDP $WG_PORT?)"; fi
  if command -v lxc >/dev/null; then C=$(lxc list -c n -f csv status=running 2>/dev/null | head -1); if [ -n "$C" ]; then IP=$(lxc exec "$C" -- curl -4 -s --max-time 8 ifconfig.me 2>/dev/null); [ -n "$IP" ] && ok "VPS '$C' ka IPv4 internet: $IP (yahi relay ka IP hona chahiye)" || bad "VPS '$C' ko IPv4 internet nahi mila"; fi; fi;;
*) sed -n 2,8p "$0"; exit 1;;
esac
echo

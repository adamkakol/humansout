#!/usr/bin/env bash
# Hole punching through real NAT, on one Linux machine and without root: two home networks, each behind its own
# router doing NAT (nftables masquerade), and an "internet" with a hub and two STUN servers, all in unprivileged
# user and network namespaces. Nothing outside the namespaces is touched.
#
#   tools/nat_lab.sh            both routers keep a socket's outside port whatever the destination (most homes)
#   tools/nat_lab.sh symmetric  bob's router picks a new random port per destination (many carrier and
#                               mobile NATs): alice sprays ports, bob probes from many sockets
#   tools/nat_lab.sh both-symmetric   both routers do: expected to fail, and to say why
#
#   internet (hub 198.18.1.1 / 198.18.2.1, STUN :3478 :3479)
#     |-- router A (198.18.1.2, NAT) -- home A 10.0.1.2: alice
#     `-- router B (198.18.2.2, NAT) -- home B 10.0.2.2: bob
set -euo pipefail
MODE="${1:-cone}"
if [ -z "${NAT_LAB_INSIDE:-}" ]; then
  exec env NAT_LAB_INSIDE=1 unshare -rn --fork bash ${NAT_LAB_TRACE:+-x} "$0" "$@"
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
export LAB="$(mktemp -d)" PYTHONPATH="$ROOT"
pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; rm -rf "$LAB"; }
trap cleanup EXIT

netns() { unshare -n sleep 3600 >/dev/null 2>&1 & pids+=($!); printf -v "$1" %s $!; }  # NAME: a new namespace
in_ns() { local pid=$1; shift; nsenter -t "$pid" -n "$@"; }

ip link set lo up
echo 1 > /proc/sys/net/ipv4/ip_forward
netns RA; netns HA; netns RB; netns HB
sleep 0.3
link() {  # link NAME_HERE NS_HERE NAME_THERE NS_THERE (NS "" = the internet)
  ip link add "$1" type veth peer name "$3"
  [ -n "$2" ] && ip link set "$1" netns "$2"
  [ -n "$4" ] && ip link set "$3" netns "$4"
  return 0
}
link inet-a "" wan-a "$RA"; link lan-a "$RA" home-a "$HA"
link inet-b "" wan-b "$RB"; link lan-b "$RB" home-b "$HB"
ip addr add 198.18.1.1/24 dev inet-a; ip link set inet-a up
ip addr add 198.18.2.1/24 dev inet-b; ip link set inet-b up
router() {  # router PID WAN LAN WAN_ADDR GATEWAY LAN_ADDR MASQUERADE_FLAGS
  in_ns "$1" sh -c "ip link set lo up; ip addr add $4/24 dev $2; ip link set $2 up; ip addr add $6/24 dev $3;
    ip link set $3 up; ip route add default via $5; echo 1 > /proc/sys/net/ipv4/ip_forward"
  in_ns "$1" nft add table ip nat
  in_ns "$1" nft "add chain ip nat post { type nat hook postrouting priority 100 ; }"
  in_ns "$1" nft add rule ip nat post oifname "$2" masquerade $7
  in_ns "$1" nft add table ip filter  # a home router: nothing comes in that was not asked for
  in_ns "$1" nft "add chain ip filter through { type filter hook forward priority 0 ; policy drop ; }"
  in_ns "$1" nft add rule ip filter through iifname "$3" accept
  in_ns "$1" nft add rule ip filter through ct state established,related accept
}
home() { in_ns "$1" sh -c "ip link set lo up; ip addr add $3/24 dev $2; ip link set $2 up; ip route add default via $4"; }
router "$RA" wan-a lan-a 198.18.1.2 198.18.1.1 10.0.1.1 "$([ "$MODE" = both-symmetric ] && echo 'random,fully-random' || true)"
router "$RB" wan-b lan-b 198.18.2.2 198.18.2.1 10.0.2.1 "$([ "$MODE" != cone ] && echo 'random,fully-random' || true)"
home "$HA" home-a 10.0.1.2 10.0.1.1
home "$HB" home-b 10.0.2.2 10.0.2.1

"$PY" "$ROOT/tools/nat_lab_agent.py" hub & pids+=($!)
sleep 1.5
in_ns "$HB" "$PY" "$ROOT/tools/nat_lab_agent.py" bob http://198.18.2.1:7400 setup > "$LAB/bob.json"
in_ns "$HA" "$PY" "$ROOT/tools/nat_lab_agent.py" alice http://198.18.1.1:7400 setup > /dev/null
echo "routers: $MODE; alice and bob cannot reach each other's homes:"
if in_ns "$HA" ping -c1 -W1 10.0.2.2 >/dev/null 2>&1; then echo "  (unexpected: home B is reachable)"; else echo "  ping 10.0.2.2 from home A: no route through"; fi
in_ns "$HB" "$PY" "$ROOT/tools/nat_lab_agent.py" bob http://198.18.2.1:7400 listen 40 & pids+=($!)
sleep 3
in_ns "$HA" "$PY" "$ROOT/tools/nat_lab_agent.py" alice http://198.18.1.1:7400 punch

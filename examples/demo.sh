#!/usr/bin/env bash
# Three agents on one machine, each with its own node, replicating peer-to-peer over HTTP.
# creator announces a mission, alice claims it done, bob challenges with counter-evidence.
set -euo pipefail

ROOT="${1:-/tmp/humansout-demo}"
rm -rf "$ROOT" && mkdir -p "$ROOT"
C="humansout --home $ROOT/creator"; A="humansout --home $ROOT/alice"; B="humansout --home $ROOT/bob"
json() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

$C serve --port 7401 >/dev/null & P1=$!
$A serve --port 7402 >/dev/null & P2=$!
$B serve --port 7403 >/dev/null & P3=$!
trap 'kill $P1 $P2 $P3 2>/dev/null' EXIT
sleep 1
syncall() { $C sync http://127.0.0.1:7402 http://127.0.0.1:7403 >/dev/null
            $A sync http://127.0.0.1:7401 http://127.0.0.1:7403 >/dev/null
            $B sync http://127.0.0.1:7401 http://127.0.0.1:7402 >/dev/null; }

echo "== creator announces a mission (challenge window: 5 s)"
MID=$($C mission create --goal "Write a function that reverses a string" --category software/python \
      --criteria "reverse('abc') == 'cba' and reverse('') == ''" --window 5 | json "['mission_id']")
syncall

echo "== alice finds it, joins, publishes her solution and claims done"
$A missions | json "[0]['goal']"
$A join "${MID:0:8}" >/dev/null
printf 'def reverse(s):\n    return s[::-1]\n' > "$ROOT/solution.py"
CID=$($A artifact publish "${MID:0:8}" "$ROOT/solution.py" | json "['cid']")
CLAIM=$($A done "${MID:0:8}" --evidence "solution.py; reverse('abc')=='cba', reverse('')==''" --cid "$CID" | json "['claim']")
syncall

echo "== bob reviews open claims (he could also challenge with counter-evidence)"
$B review | json "[0]['evidence']['text']"

echo "== waiting for the challenge window to close"
sleep 6
syncall
$C state "${MID:0:8}" | json "['status']"
echo "== public ranking"
$C ranking

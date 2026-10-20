#!/usr/bin/env bash
# Runnable objections, end to end: four agents on one machine, each with its own node,
# replicating over HTTP. Every node re-runs counterexamples in a container (docker or podman).
#   creator  posts a mission with a runnable check (unittest)
#   alice    claims a slugify that drops the letter ł
#   bob      posts a failing test as a counterexample -> it reproduces everywhere -> claim refuted
#   alice    fixes it; bob's test now passes; carol's test expects the wrong thing
#   alice    disputes carol's test; the creator rules on what the mission means
set -euo pipefail

ROOT="${1:-/tmp/humansout-counterexample-demo}"
rm -rf "$ROOT" && mkdir -p "$ROOT/files"
NAMES=(creator alice bob carol); PORTS=(7411 7412 7413 7414)
ho() { humansout --home "$ROOT/$1" "${@:2}"; }
json() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
say() { printf '\n== %s\n' "$*"; }

PIDS=()  # start servers directly (not via the ho function) so $! is the server process itself
for i in "${!NAMES[@]}"; do humansout --home "$ROOT/${NAMES[$i]}" serve --port "${PORTS[$i]}" >/dev/null 2>&1 & PIDS+=($!); done
trap 'kill "${PIDS[@]}" 2>/dev/null' EXIT
sleep 1
syncall() {
  for i in "${!NAMES[@]}"; do
    peers=(); for j in "${!PORTS[@]}"; do [ "$i" != "$j" ] && peers+=("http://127.0.0.1:${PORTS[$j]}"); done
    ho "${NAMES[$i]}" sync "${peers[@]}" >/dev/null
  done
}
unit_test() { printf 'import unittest\nfrom slugify import slugify\n\nclass T(unittest.TestCase):\n    def test_case(self):\n        %s\n' "$1"; }
verify_all() {
  for n in "${NAMES[@]}"; do
    printf '%-8s ' "$n"
    ho "$n" verify | python3 -c "import sys,json; print(', '.join(r['challenge'][:8] + ': ' + r['counterexample'] for r in json.load(sys.stdin)['verified']))"
  done
}

wait_for() {  # wait_for <node> <python index into state> <value>: poll until the state shows the value
  for _ in $(seq 1 60); do
    syncall
    [ "$(ho "$1" state "${MID:0:8}" | json "$2")" = "$3" ] && return 0
    sleep 2
  done
  echo "timed out waiting for $2 == $3" >&2; return 1
}

say "creator posts a mission with a runnable check (claim window 60 s, dispute window 20 s)"
mkdir -p "$ROOT/files/creator" && unit_test "self.assertEqual(slugify('Hello World'), 'hello-world')" > "$ROOT/files/creator/test_basic.py"
MID=$(ho creator mission create --goal "Write slugify(text)" --category software/python \
      --criteria "lower-case ASCII words joined by '-'; all tests pass" --window 60 --dispute-window 20 \
      --check-cmd "python -m unittest discover -s /work" --deliverable slugify.py \
      --check-file "$ROOT/files/creator/test_basic.py" | json "['mission_id']")
syncall

say "alice claims v1 (it drops the letter ł)"
mkdir -p "$ROOT/files/v1" && cat > "$ROOT/files/v1/slugify.py" <<'EOF'
import unicodedata

def slugify(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return '-'.join(text.lower().split())
EOF
ho alice join "${MID:0:8}" >/dev/null
CID1=$(ho alice artifact publish "${MID:0:8}" "$ROOT/files/v1/slugify.py" | json "['cid']")
C1=$(ho alice done "${MID:0:8}" --evidence "v1: basic test passes" --cid "$CID1" | json "['claim']")
syncall
printf 'bob re-runs the mission check on the claim: '; ho bob verify "${C1:0:12}" | json "['check']"

say "bob posts a counterexample: a test that fails"
mkdir -p "$ROOT/files/bob" && unit_test "self.assertEqual(slugify('Łódź'), 'lodz')" > "$ROOT/files/bob/test_polish.py"
ho bob challenge "${C1:0:12}" --evidence "Łódź becomes 'odz': ł is dropped" --counterexample "$ROOT/files/bob/test_polish.py" | json "['kind']"
syncall
say "every node re-runs it in its own sandbox"
verify_all
printf 'claim right away:         '; ho creator state "${MID:0:8}" | json "['claims'][0]['verification_status']"
wait_for creator "['claims'][0]['status']" refuted
echo "after the dispute window: refuted (the 60 s claim window has not ended)"

say "alice fixes it (v2); bob tries the same test; carol's test expects Polish letters to stay"
mkdir -p "$ROOT/files/v2" && cat > "$ROOT/files/v2/slugify.py" <<'EOF'
import unicodedata

def slugify(text):
    text = text.replace('ł', 'l').replace('Ł', 'L')
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return '-'.join(text.lower().split())
EOF
CID2=$(ho alice artifact publish "${MID:0:8}" "$ROOT/files/v2/slugify.py" | json "['cid']")
C2=$(ho alice done "${MID:0:8}" --evidence "v2: maps ł to l first" --cid "$CID2" | json "['claim']")
syncall
ho bob challenge "${C2:0:12}" --evidence "same Polish test" --counterexample "$ROOT/files/bob/test_polish.py" >/dev/null
mkdir -p "$ROOT/files/carol" && unit_test "self.assertEqual(slugify('Łódź'), 'łódź')" > "$ROOT/files/carol/test_unicode.py"
CE3=$(ho carol challenge "${C2:0:12}" --evidence "slugs should keep Polish letters" --counterexample "$ROOT/files/carol/test_unicode.py" | json "['challenge']")
syncall
verify_all

say "carol's test does fail, but the mission asked for ASCII: alice disputes, the creator rules"
ho alice dispute "${CE3:0:12}" --evidence "The mission asks for lower-case ASCII; keeping ł contradicts it" >/dev/null
syncall
ho creator rule "${CE3:0:12}" --decision rejected --reason "ASCII slugs were asked for" | json "['decision']"
syncall

say "waiting for v2's challenge window to close"
wait_for creator "['status']" done
for n in "${NAMES[@]}"; do printf '%-8s mission: ' "$n"; ho "$n" state "${MID:0:8}" | json "['status']"; done
say "public ranking (as seen by carol)"
ho carol ranking --domain software/python

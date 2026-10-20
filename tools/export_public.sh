#!/usr/bin/env bash
# Export the files meant for a public repository into a fresh folder, for review before publishing.
# Other experiments in this checkout hold local paths and raw runs; they are left out on purpose.
set -euo pipefail
cd "$(dirname "$0")/.."
out="${1:-dist/public-tree}"
rm -rf "$out" && mkdir -p "$out"
paths=(humansout tests docs site examples tools .github pyproject.toml README.md LICENSE SECURITY.md CONTRIBUTING.md CHANGELOG.md .gitignore
       experiments/h4_runnable experiments/replay_study/README.md experiments/replay_study/collect.py
       experiments/replay_study/phase1-20261020/summary.md missions/sejm-2023-robustness)
git ls-files -z -- "${paths[@]}" | xargs -0 -I{} cp --parents {} "$out"
echo "exported $(find "$out" -type f | wc -l) files to $out"
if grep -rIil -e "/home/adam" -e "dgx" -e "spark-d8fd" -e "ryszard" "$out" | grep -v "tools/export_public.sh"; then
  echo "^ these files mention local paths or hosts: review them before publishing" >&2
  exit 1
fi

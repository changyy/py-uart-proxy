#!/bin/sh
# The tests under every Python CI runs (.github/workflows/ci.yml), in parallel:
# one process per version, each in an isolated uv environment with this
# checkout installed (editable) and pytest. A failure prints that version's
# output and fails the whole run.
#   scripts/test-pythons.sh            all of them
#   scripts/test-pythons.sh 3.10       just one
cd "$(dirname "$0")/.." || exit 1
VERSIONS="${*:-3.10 3.11 3.12 3.13}"
LOGS="$(mktemp -d)"
trap 'rm -rf "$LOGS"' EXIT
for v in $VERSIONS; do
  ( uv run --quiet --isolated --python "$v" --with-editable ".[dev]" \
      python -m pytest -q -p no:cacheprovider --color=no -rf > "$LOGS/$v.log" 2>&1
    echo $? > "$LOGS/$v.code" ) &
done
wait
failed=""
for v in $VERSIONS; do
  code="$(cat "$LOGS/$v.code" 2>/dev/null || echo 1)"
  summary="$(tail -n 1 "$LOGS/$v.log")"
  echo "==> Python $v: $summary" >&2
  if [ "$code" != "0" ]; then
    failed="$failed $v"
    grep -E "^(FAILED|ERROR) " "$LOGS/$v.log" >&2
    grep -B 30 -E "^E  " "$LOGS/$v.log" | grep -E "^(tests/|E  |    def |>)" | head -30 >&2
  fi
done
if [ -n "$failed" ]; then
  echo "==> failed on:$failed" >&2
  exit 1
fi
echo "==> all passed: $VERSIONS" >&2

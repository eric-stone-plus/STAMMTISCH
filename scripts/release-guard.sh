#!/usr/bin/env bash
# Thin loader: the guard logic lives in the sibling release-kit repo,
# pinned by commit SHA (same digest-pinning discipline as the SHA-pinned
# marketplace actions in release.yml). Re-pin: review the release-kit
# change, then update RELEASE_KIT_PIN to its full commit SHA.
set -euo pipefail
RELEASE_KIT_PIN="81aba74b4826ba5cb4670901dca9287ca82a4ac2"
here="$(cd "$(dirname "$0")" && pwd)"
kit="${RELEASE_KIT_HOME:-$here/../../release-kit}"
if [ ! -d "$kit/.git" ]; then
  printf 'release guard: release-kit checkout missing at %s\n' "$kit" >&2
  printf '  clone eric-stone-plus/release-kit next to this repo, or set RELEASE_KIT_HOME\n' >&2
  exit 1
fi
head_sha="$(git -C "$kit" rev-parse HEAD)"
if [ "$head_sha" != "$RELEASE_KIT_PIN" ]; then
  printf 'release guard: release-kit HEAD %s != pinned %s\n' "$head_sha" "$RELEASE_KIT_PIN" >&2
  printf '  review the release-kit change, then update RELEASE_KIT_PIN in scripts/release-guard.sh\n' >&2
  exit 1
fi
export RELEASE_GUARD_CONF="$here/release-guard.conf"
exec bash "$kit/release-guard.sh" "$@"

#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'release guard: %s\n' "$*" >&2
  exit 1
}

usage() {
  printf 'usage: %s VERSION CANDIDATE_SHA | %s --assets DIST [--checksums]\n' "$0" "$0" >&2
  exit 2
}

verify_assets() {
  local directory="${1:-}" require_checksums="${2:-}" file name expected_list actual_list
  local -a expected
  [[ -d "$directory" ]] || die "asset directory is unavailable: $directory"
  expected=(
    stammtisch-core-aarch64-apple-darwin.tar.gz
    stammtisch-core-aarch64-unknown-linux-gnu.tar.gz
    stammtisch-core-x86_64-apple-darwin.tar.gz
    stammtisch-core-x86_64-unknown-linux-gnu.tar.gz
  )
  if [[ "$require_checksums" == "--checksums" ]]; then
    expected+=(SHA256SUMS)
  elif [[ -n "$require_checksums" ]]; then
    usage
  fi

  while IFS= read -r -d '' file; do
    [[ -f "$file" && ! -L "$file" && -s "$file" ]] || die "asset is not a non-empty regular file: $file"
    name="${file##*/}"
    actual_list+="${actual_list:+$'\n'}$name"
  done < <(find "$directory" -mindepth 1 -maxdepth 1 -print0)

  expected_list="$(printf '%s\n' "${expected[@]}" | LC_ALL=C sort)"
  actual_list="$(printf '%s\n' "${actual_list:-}" | sed '/^$/d' | LC_ALL=C sort)"
  [[ "$(printf '%s\n' "$actual_list" | sed '/^$/d' | wc -l | tr -d '[:space:]')" -eq "${#expected[@]}" ]] \
    || die "asset count differs from the exact release set"
  [[ "$actual_list" == "$expected_list" ]] \
    || die "asset names differ from the exact release set"

  if [[ "$require_checksums" == "--checksums" ]]; then
    (
      cd "$directory"
      checksum_output="$(sha256sum --check --strict SHA256SUMS 2>&1)"
      checksum_status=$?
      [[ "$checksum_status" -eq 0 ]] || {
        printf '%s\n' "$checksum_output" >&2
        exit "$checksum_status"
      }
      [[ "$(wc -l < SHA256SUMS | tr -d '[:space:]')" == 4 ]]
      for file in "${expected[@]}"; do
        [[ "$file" == SHA256SUMS ]] && continue
        [[ "$(awk -v name="$file" '$2 == name {count++} END {print count+0}' SHA256SUMS)" == 1 ]]
      done
    ) || die "SHA256SUMS does not bind each archive exactly once"
  fi
  printf 'release guard: exact asset set is valid\n'
}

if [[ "${1:-}" == "--assets" ]]; then
  verify_assets "${2:-}" "${3:-}"
  exit 0
fi

version="${1:-}"
candidate="${2:-}"
[[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || usage
[[ "$candidate" =~ ^[0-9a-f]{40}$ ]] || die "candidate must be a full lowercase commit SHA"

tag="v$version"
repository="${GITHUB_REPOSITORY:-}"
history="${RELEASE_GUARD_HISTORY:-.github/release-history.txt}"
current_run_id="${GITHUB_RUN_ID:-0}"

[[ -f "$history" ]] || die "immutable release history is unavailable"

package_version="$({ cargo metadata --locked --no-deps --format-version 1 || die "cargo metadata failed"; } | jq -er '.packages[] | select(.name == "stammtisch") | .version')" \
  || die "Cargo package version is unavailable"
[[ "$version" == "$package_version" ]] \
  || die "requested $version does not match Cargo version $package_version"

version_key() {
  local value="$1" major minor patch extra
  IFS=. read -r major minor patch extra <<<"$value"
  [[ -z "${extra:-}" && "$major" =~ ^(0|[1-9][0-9]*)$ && "$minor" =~ ^(0|[1-9][0-9]*)$ && "$patch" =~ ^(0|[1-9][0-9]*)$ ]] \
    || die "invalid stable version in immutable history: $value"
  printf '%020d%020d%020d' "$major" "$minor" "$patch"
}

requested_key="$(version_key "$version")"
max_used_key=''
while IFS= read -r used; do
  [[ -n "$used" ]] || continue
  used_key="$(version_key "$used")"
  [[ "$used_key" != "$requested_key" ]] || die "$version is already burned in immutable release history"
  if [[ -z "$max_used_key" || "$used_key" > "$max_used_key" ]]; then
    max_used_key="$used_key"
  fi
done < <(awk '!/^[[:space:]]*#/ && NF {print $1}' "$history")
if [[ -n "$max_used_key" ]]; then
  [[ "$requested_key" > "$max_used_key" ]] \
    || die "$version is not strictly newer than every burned version"
fi

git cat-file -e "$candidate^{commit}" 2>/dev/null || die "candidate is not a local commit"
[[ "$(git rev-parse "$candidate^{commit}")" == "$candidate" ]] || die "candidate is not a commit object"

if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  [[ "${GITHUB_EVENT_NAME:-}" == "workflow_dispatch" ]] || die "release must use workflow_dispatch"
  [[ "${GITHUB_REF:-}" == "refs/heads/main" ]] || die "release must be dispatched from main"
  [[ "${GITHUB_SHA:-}" == "$candidate" ]] || die "candidate differs from the dispatch SHA"
  [[ "${GITHUB_RUN_ATTEMPT:-}" == 1 ]] || die "release workflow reruns are forbidden; choose a new version"
fi

origin_main="$(git rev-parse refs/remotes/origin/main 2>/dev/null)" || die "origin/main is unavailable"
git merge-base --is-ancestor "$candidate" "$origin_main" \
  || die "candidate is not an ancestor of origin/main"

[[ -n "$repository" ]] || die "GITHUB_REPOSITORY is unavailable"

api_lookup() {
  local endpoint="$1" destination="$2" raw rc status
  raw="$(mktemp)"
  if [[ -n "${RELEASE_GUARD_OFFLINE:-}" ]]; then
    if "${RELEASE_GUARD_FAKE_API:?missing RELEASE_GUARD_FAKE_API}" LOOKUP "$endpoint" >"$raw" 2>/dev/null; then
      rc=0
    else
      rc=$?
    fi
  else
    [[ -n "${GH_TOKEN:-}" ]] || die "GH_TOKEN is unavailable"
    if gh api --include "$endpoint" >"$raw" 2>/dev/null; then
      rc=0
    else
      rc=$?
    fi
  fi
  status="$(sed -nE 's/^HTTP\/[^ ]+ ([0-9]{3}).*/\1/p' "$raw" | tail -1)"
  [[ "$status" =~ ^[0-9]{3}$ ]] || { rm -f "$raw"; return 90; }
  if [[ "$status" == 404 ]]; then
    rm -f "$raw"
    [[ "$rc" -ne 0 ]] || return 91
    return 44
  fi
  if [[ "$status" =~ ^2[0-9]{2}$ && "$rc" -eq 0 ]]; then
    awk '
      /^HTTP\/[^ ]+ [0-9][0-9][0-9]/ {body=""; headers=1; next}
      headers && /^\r?$/ {headers=0; next}
      !headers {body=body $0 ORS}
      END {printf "%s", body}
    ' "$raw" >"$destination"
    rm -f "$raw"
    [[ -s "$destination" ]] || return 92
    return 0
  fi
  rm -f "$raw"
  return 93
}

api_list() {
  local endpoint="$1" destination="$2" rc
  if [[ -n "${RELEASE_GUARD_OFFLINE:-}" ]]; then
    if "${RELEASE_GUARD_FAKE_API:?missing RELEASE_GUARD_FAKE_API}" LIST "$endpoint" >"$destination" 2>/dev/null; then
      rc=0
    else
      rc=$?
    fi
  else
    [[ -n "${GH_TOKEN:-}" ]] || die "GH_TOKEN is unavailable"
    if gh api --paginate --slurp "$endpoint" >"$destination" 2>/dev/null; then
      rc=0
    else
      rc=$?
    fi
  fi
  [[ "$rc" -eq 0 && -s "$destination" ]]
}

ref_endpoint="repos/$repository/git/ref/tags/$tag"
release_endpoint="repos/$repository/releases/tags/$tag"

api_tmp="$(mktemp -d)"
trap 'rm -rf "$api_tmp"' EXIT
if api_lookup "$ref_endpoint" "$api_tmp/tag.json"; then
  tag_rc=0
else
  tag_rc=$?
fi
if [[ "$tag_rc" -eq 0 ]]; then
  jq -e --arg ref "refs/tags/$tag" '.ref == $ref and (.object.sha | type == "string")' "$api_tmp/tag.json" >/dev/null \
    || die "tag lookup returned malformed data"
  die "tag $tag already exists"
elif [[ "$tag_rc" -ne 44 ]]; then
  die "tag lookup failed (status $tag_rc); only an exact 404 is absence"
fi

if api_lookup "$release_endpoint" "$api_tmp/release.json"; then
  release_rc=0
else
  release_rc=$?
fi
if [[ "$release_rc" -eq 0 ]]; then
  jq -e --arg tag "$tag" '.tag_name == $tag' "$api_tmp/release.json" >/dev/null \
    || die "release lookup returned malformed data"
  die "release $tag already exists"
elif [[ "$release_rc" -ne 44 ]]; then
  die "release lookup failed (status $release_rc); only an exact 404 is absence"
fi

api_list "repos/$repository/git/matching-refs/tags/v?per_page=100" "$api_tmp/tags.json" \
  || die "tag history lookup failed"
api_list "repos/$repository/releases?per_page=100" "$api_tmp/releases.json" \
  || die "release history lookup failed"
api_list "repos/$repository/actions/workflows/release.yml/runs?event=workflow_dispatch&per_page=100" "$api_tmp/runs.json" \
  || die "Actions history lookup failed"

jq -e '
  type == "array" and all(.[]; type == "array") and
  all(.[][]; (.ref | type == "string"))
' "$api_tmp/tags.json" >/dev/null || die "tag history returned malformed data"
jq -e '
  type == "array" and all(.[]; type == "array") and
  all(.[][]; (.tag_name | type == "string"))
' "$api_tmp/releases.json" >/dev/null || die "release history returned malformed data"

while IFS= read -r used; do
  [[ -n "$used" ]] || continue
  used_key="$(version_key "$used")"
  [[ "$used_key" != "$requested_key" ]] || die "$tag already exists in tag or release history"
  [[ "$used_key" < "$requested_key" ]] || die "$version is not newer than historical version $used"
done < <(jq -nr --slurpfile tags "$api_tmp/tags.json" --slurpfile releases "$api_tmp/releases.json" '
  ($tags[0][][] | .ref | select(test("^refs/tags/v[0-9]+\\.[0-9]+\\.[0-9]+$")) | sub("^refs/tags/v"; "")),
  ($releases[0][][] | .tag_name | select(test("^v[0-9]+\\.[0-9]+\\.[0-9]+$")) | sub("^v"; ""))
')

jq -e '
  if type == "array" then
    all(.[]; (.total_count | type == "number") and (.workflow_runs | type == "array")) and
    ((map(.workflow_runs | length) | add // 0) >= (.[0].total_count // 0))
  else
    (.total_count | type == "number") and (.workflow_runs | type == "array") and
    ((.workflow_runs | length) >= .total_count)
  end
' "$api_tmp/runs.json" >/dev/null \
  || die "Actions history returned malformed data"
if jq -e \
  --arg candidate "$candidate" --argjson current "$current_run_id" '
    (if type == "array" then [.[].workflow_runs[]] else .workflow_runs end)[] |
    select(
      (.id | type != "number") or
      (.head_sha | type != "string") or
      (.display_title | type != "string") or
      (.event | type != "string") or
      (.event != "workflow_dispatch") or
      (.display_title | test("^Release v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*) from [0-9a-f]{40}$") | not) or
      (.id != $current and .head_sha == $candidate)
    )
  ' "$api_tmp/runs.json" >/dev/null; then
  die "candidate $candidate was already attempted, or Actions history is malformed"
fi

while IFS= read -r attempted; do
  [[ -n "$attempted" ]] || continue
  attempted_key="$(version_key "$attempted")"
  [[ "$attempted_key" != "$requested_key" ]] || die "$tag was already attempted by another release run"
  [[ "$attempted_key" < "$requested_key" ]] \
    || die "$version is not strictly newer than attempted version $attempted"
done < <(jq -r --argjson current "$current_run_id" '
  (if type == "array" then [.[].workflow_runs[]] else .workflow_runs end)[] |
  select(.id != $current) |
  .display_title |
  capture("^Release v(?<version>[0-9]+\\.[0-9]+\\.[0-9]+) from [0-9a-f]{40}$").version
' "$api_tmp/runs.json")

printf 'release guard: %s is bound to %s and eligible\n' "$tag" "$candidate"

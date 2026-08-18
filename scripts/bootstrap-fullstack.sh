#!/usr/bin/env sh
# Bootstrap the STAMMTISCH base workstation.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/eric-stone-plus/STAMMTISCH/main/scripts/bootstrap-fullstack.sh | sh
#
# The one-click install is deliberately the base platform only:
# STAMMTISCH core (Rust) and its shipped examples. The review
# orchestrator (QUINTE), the delivery rules plane (HIGHBALL), the
# analysis identity (GALAHAD), and the philosophy (RASHOMON) are
# separate repositories installed deliberately — see
# docs/fullstack-quickstart.md for the full-stack path.
#
# It never writes credentials and never installs anything outside the
# workspace directory.
#
# Usage: sh bootstrap-fullstack.sh [WORKSPACE_DIR]

set -eu

WORKSPACE="${1:-$HOME/stammtisch-workstation}"
ORG="https://github.com/eric-stone-plus"
mkdir -p "$WORKSPACE"
cd "$WORKSPACE"

if [ -d STAMMTISCH ]; then
    echo "== STAMMTISCH exists; pulling"
    git -C STAMMTISCH pull --ff-only
else
    echo "== cloning STAMMTISCH"
    git clone "$ORG/STAMMTISCH.git"
fi

echo "== building STAMMTISCH core"
cargo build --release --manifest-path STAMMTISCH/Cargo.toml

cat <<EOF

Base workstation ready: $WORKSPACE/STAMMTISCH

  export STAMMTISCH_HOME=\$HOME/.local/share/stammtisch
  $WORKSPACE/STAMMTISCH/target/release/stammtisch-core init
  $WORKSPACE/STAMMTISCH/target/release/stammtisch-core validate \\
    --pipeline $WORKSPACE/STAMMTISCH/pipelines/examples/security.json

The full stack (QUINTE review, HIGHBALL authorization, GALAHAD
analysis, RASHOMON) is installed deliberately, not by this script:
see STAMMTISCH/docs/fullstack-quickstart.md.
EOF

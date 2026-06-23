#!/bin/bash
# SessionStart hook: ensure the beads (bd) issue tracker is installed.
#
# Runs only in Claude Code on the web (the remote execution environment),
# where the container is provisioned fresh. The container state is cached
# after this hook completes, so the (slow, first-run) compile happens once.
set -euo pipefail

# Only needed in the remote (web) environment; a no-op locally.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Where `go install` lands the binary (the installer's fallback path; the
# prebuilt-release download needs api.github.com, which is blocked here).
GOBIN="$(go env GOPATH 2>/dev/null || echo "$HOME/go")/bin"

# Make likely install locations visible for the rest of this hook.
for d in /usr/local/bin "$HOME/.local/bin" "$GOBIN"; do
  case ":$PATH:" in *":$d:"*) ;; *) PATH="$PATH:$d" ;; esac
done
export PATH

# Idempotent: install only if bd isn't already on PATH.
if ! command -v bd >/dev/null 2>&1; then
  echo "Installing beads (bd)..." >&2
  # The official installer exits non-zero with a PATH warning even when the
  # `go install` fallback succeeds, so don't let it fail the hook (set -e).
  curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash || true
fi

# Persist the install location on PATH for the agent's shell this session.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"\$PATH:/usr/local/bin:$HOME/.local/bin:$GOBIN\"" >> "$CLAUDE_ENV_FILE"
fi

# Verify the binary is usable.
if ! command -v bd >/dev/null 2>&1; then
  echo "Error: beads (bd) was not found on PATH after install." >&2
  exit 1
fi
bd version >&2

# Hydrate the local Dolt issue database from the committed JSONL export.
#
# The container is provisioned fresh, so the (gitignored) embedded Dolt DB
# doesn't exist and `bd` commands fail with "no beads database found". The
# configured Dolt remote (sync.remote in .beads/config.yaml) is a git+ssh
# URL that isn't reachable from this sandbox, so we rebuild the DB locally
# from .beads/issues.jsonl -- beads' off-machine recovery path. `--remote ""`
# overrides the configured SSH remote for this init only (it does not touch
# the tracked config.yaml). Idempotent: skipped once the DB exists (cached).
if [ -f "$CLAUDE_PROJECT_DIR/.beads/issues.jsonl" ] && \
   ! ( cd "$CLAUDE_PROJECT_DIR" && bd list >/dev/null 2>&1 ); then
  echo "Hydrating beads database from issues.jsonl..." >&2
  # --skip-agents/--skip-hooks keep init from re-running project integration
  # (it would otherwise rewrite CLAUDE.md/AGENTS.md/settings.json and install
  # git hooks on every fresh container). We only want the local DB + import.
  if ( cd "$CLAUDE_PROJECT_DIR" && BD_NON_INTERACTIVE=1 \
       bd init --from-jsonl --prefix topovert --remote "" \
               --skip-agents --skip-hooks --non-interactive ) >&2; then
    echo "Beads database hydrated." >&2
  else
    echo "Warning: beads DB hydration failed; rebuild manually with" \
         "'bd init --from-jsonl --prefix topovert --remote \"\"'." >&2
  fi
fi

#!/usr/bin/env bash
# Set up GitHub push credentials for the Pi running the Site Admin.
# Run once on each machine that publishes edits to bankstgrinders.com.
#
# Prerequisite: a GitHub fine-grained Personal Access Token with:
#   - Repository access: bankstgrinders/bankstgrinders (only)
#   - Repository permissions: Contents = Read and write
# Create one at: https://github.com/settings/personal-access-tokens/new
#
# Stores the token at ~/.config/bankstgrinders/git-credentials (mode 600)
# and points git at it via a repo-local credential.helper, so the user's
# global ~/.git-credentials is never touched.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [ ! -d .git ]; then
  echo "ERROR: $REPO_DIR is not a git checkout." >&2
  exit 1
fi

REMOTE_URL="$(git remote get-url origin 2>/dev/null || true)"
case "$REMOTE_URL" in
  https://github.com/*) ;;
  *) echo "ERROR: origin must be an HTTPS GitHub URL (got: $REMOTE_URL)." >&2; exit 1 ;;
esac

echo "Repo:   $REPO_DIR"
echo "Remote: $REMOTE_URL"
echo
echo "Paste your GitHub fine-grained PAT (input is hidden):"
IFS= read -rs PAT
echo
if [ -z "${PAT:-}" ]; then
  echo "ERROR: empty token; aborting." >&2
  exit 1
fi

CRED_DIR="$HOME/.config/bankstgrinders"
CRED_FILE="$CRED_DIR/git-credentials"
mkdir -p "$CRED_DIR"
chmod 700 "$CRED_DIR"

# Single-line credential file scoped to github.com.
umask 077
printf 'https://x-access-token:%s@github.com\n' "$PAT" > "$CRED_FILE"
chmod 600 "$CRED_FILE"

# Repo-local config only — never touches global git settings.
git config --local user.name  "Bank St. Grinders Admin"
git config --local user.email "admin@bankstgrinders.com"
git config --local credential.helper "store --file=$CRED_FILE"

echo "Testing credentials with a remote read..."
if git ls-remote --heads origin master >/dev/null 2>&1; then
  echo "OK: GitHub read access works."
else
  echo "WARNING: ls-remote failed. Check the token's repo access and try again." >&2
  exit 1
fi

echo
echo "Setup complete."
echo "Site Admin (http://<this-pi>:8080/tv/site-admin.html) can now publish edits."
echo "Rotate the token by re-running this script."

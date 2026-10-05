#!/usr/bin/env bash
# The repo is public, so everything private (data/leads.db, data/suppression.txt, the resume) lives encrypted
# on the single-commit branch `state`, as vault.tar.gz.enc (AES-256, key = SMA_VAULT_KEY).
#   scripts/vault.sh open   fetch `state` and decrypt into data/ and profile/
#   scripts/vault.sh save   encrypt data/ + resume and force-push them as the new `state`
# The GitHub workflow runs open -> sma run -> save. Locally the key is read from .env.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${SMA_VAULT_KEY:-}" ] && [ -f .env ]; then
  SMA_VAULT_KEY=$(grep -E '^SMA_VAULT_KEY=' .env | cut -d= -f2- | tr -d '\r' || true)
fi
: "${SMA_VAULT_KEY:?SMA_VAULT_KEY is not set (GitHub secret, or .env locally)}"
export SMA_VAULT_KEY

FILES=(data/leads.db data/suppression.txt profile/Shantanu_resume.pdf)
crypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:SMA_VAULT_KEY "$@"; }

case "${1:-}" in
  open)
    git fetch -q origin state
    git show origin/state:vault.tar.gz.enc > .vault.tmp
    crypt -d -in .vault.tmp | tar xzf -
    rm -f .vault.tmp
    echo "vault opened: $(git log -1 --format=%s origin/state)"
    ;;
  save)
    present=()
    for f in "${FILES[@]}"; do [ -f "$f" ] && present+=("$f"); done
    tar czf - "${present[@]}" | crypt -out .vault.tmp
    blob=$(git hash-object -w .vault.tmp)
    rm -f .vault.tmp
    tree=$(printf '100644 blob %s\tvault.tar.gz.enc\n' "$blob" | git mktree)
    commit=$(GIT_AUTHOR_NAME=sma GIT_AUTHOR_EMAIL=sma@users.noreply.github.com \
             GIT_COMMITTER_NAME=sma GIT_COMMITTER_EMAIL=sma@users.noreply.github.com \
             git commit-tree "$tree" -m "state $(date -u +%Y-%m-%dT%H:%M:%SZ)")
    for _ in 1 2 3; do
      if git push -q -f origin "$commit:refs/heads/state"; then echo "vault saved: ${present[*]}"; exit 0; fi
      sleep 5
    done
    echo "vault save FAILED" >&2
    exit 1
    ;;
  *)
    echo "usage: scripts/vault.sh open|save" >&2
    exit 2
    ;;
esac

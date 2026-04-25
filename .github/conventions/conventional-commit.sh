#!/usr/bin/env bash
# conventional-commit.sh — interactive helper for Conventional Commits 1.0.0
#
# Drives the workflow demonstrated in this repo:
#   1. Stage changes (or rely on already-staged ones).
#   2. Run this script — it prompts for type / scope / description / body /
#      footers, builds a spec-compliant message in a temp file, and invokes
#      `git commit -F <file>` (avoiding fragile shell-string quoting).
#   3. Reject malformed input early instead of letting `git commit` fail.
#
# Reference: .github/conventions/conventional-commits.instructions.md
#
# Usage:
#   .github/conventions/conventional-commit.sh                 # interactive
#   .github/conventions/conventional-commit.sh --check FILE    # lint a msg
#   .github/conventions/conventional-commit.sh --dry-run       # print only
#   .github/conventions/conventional-commit.sh --amend         # passthrough
#
# Exits non-zero on validation errors.

set -euo pipefail

# ── Allowed types (keep in sync with conventional-commits.instructions.md) ──
ALLOWED_TYPES=(
  feat fix docs style refactor perf test build ci chore revert
)

# ── Limits ─────────────────────────────────────────────────────────────────
HEADER_SOFT_LIMIT=72
HEADER_HARD_LIMIT=100
BODY_WRAP=72

# ── Helpers ────────────────────────────────────────────────────────────────
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
warn() { printf 'warn:  %s\n' "$*" >&2; }
info() { printf '       %s\n' "$*" >&2; }

is_allowed_type() {
  local t=$1
  for a in "${ALLOWED_TYPES[@]}"; do [[ $a == "$t" ]] && return 0; done
  return 1
}

# Validate a header line ("<type>[(scope)][!]: <description>").
validate_header() {
  local hdr=$1
  local len=${#hdr}

  (( len <= HEADER_HARD_LIMIT )) \
    || die "header is ${len} chars, must be ≤ ${HEADER_HARD_LIMIT}"
  (( len <= HEADER_SOFT_LIMIT )) \
    || warn "header is ${len} chars, SHOULD be ≤ ${HEADER_SOFT_LIMIT}"

  # type[(scope)][!]: <description>
  local re='^([a-z]+)(\(([a-z0-9][a-z0-9-]*)\))?(!)?: (.+)$'
  [[ $hdr =~ $re ]] \
    || die "header does not match '<type>[(scope)][!]: <description>'"

  local type=${BASH_REMATCH[1]}
  is_allowed_type "$type" \
    || die "type '$type' not in: ${ALLOWED_TYPES[*]}"

  local desc=${BASH_REMATCH[5]}
  if [[ $desc =~ \.$ ]]; then
    warn "description ends with '.', drop the period"
  fi
  if [[ $desc =~ ^[A-Z] ]]; then
    warn "description starts upper-case, prefer lower"
  fi
}

# Validate a full commit message file: header, blank-line sep, body wrap.
validate_file() {
  local file=$1
  [[ -s $file ]] || die "$file is empty"

  local lineno=0 header='' saw_blank_after_header=0
  while IFS= read -r line || [[ -n $line ]]; do
    lineno=$((lineno + 1))
    case $lineno in
      1) header=$line ;;
      2) [[ -z $line ]] || die "line 2 must be blank (separator)"
         saw_blank_after_header=1 ;;
      *)
        # Soft-warn on body lines >72 chars (footers/URLs allowed to overflow).
        if (( ${#line} > BODY_WRAP )) && [[ $line != *' '* ]]; then
          : # single long token (URL, hash) — fine
        elif (( ${#line} > BODY_WRAP )); then
          warn "line ${lineno} is ${#line} chars, SHOULD wrap at ${BODY_WRAP}"
        fi
        ;;
    esac
  done < "$file"

  validate_header "$header"
  if (( lineno > 1 && saw_blank_after_header == 0 )); then
    die "header must be followed by a blank line before the body"
  fi
}

print_types() {
  cat >&2 <<EOF
  feat     - new feature        (SemVer MINOR)
  fix      - bug fix            (SemVer PATCH)
  docs     - documentation only
  style    - formatting / whitespace
  refactor - neither feat nor fix
  perf     - performance
  test     - tests
  build    - build system / deps
  ci       - CI configuration
  chore    - other (no src/test changes)
  revert   - revert previous commit
EOF
}

# ── --check mode ───────────────────────────────────────────────────────────
if [[ ${1:-} == --check ]]; then
  [[ -n ${2:-} ]] || die "usage: $0 --check <file>"
  validate_file "$2"
  printf 'ok: %s is conventional-commits compliant\n' "$2"
  exit 0
fi

# ── Interactive mode ───────────────────────────────────────────────────────
DRY_RUN=0
GIT_EXTRA_ARGS=()
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
    --amend|--signoff|-s|--no-verify) GIT_EXTRA_ARGS+=("$arg") ;;
    *) die "unknown flag: $arg" ;;
  esac
done

# Show what's about to be committed.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if git diff --cached --quiet; then
    warn "nothing staged; will fall through to 'git commit' which will fail"
  else
    info "staged changes:"
    git --no-pager diff --cached --stat | sed 's/^/       /' >&2
  fi
fi

# Type
echo >&2
echo "Select a type:" >&2
print_types
read -rp "type: " TYPE
TYPE=${TYPE,,}
is_allowed_type "$TYPE" || die "type '$TYPE' not in: ${ALLOWED_TYPES[*]}"

# Scope (optional)
read -rp "scope (optional, lower-kebab, blank to skip): " SCOPE
SCOPE=${SCOPE,,}
if [[ -n $SCOPE ]]; then
  [[ $SCOPE =~ ^[a-z0-9][a-z0-9-]*$ ]] \
    || die "scope must be lower-kebab-case"
fi

# Breaking change?
read -rp "breaking change? [y/N]: " BREAK
BREAK=${BREAK,,}
BANG=''
[[ $BREAK == y || $BREAK == yes ]] && BANG='!'

# Description
read -rp "description (imperative, lower-case, no trailing period): " DESC
[[ -n $DESC ]] || die "description is required"

# Build header
HEADER="$TYPE"
[[ -n $SCOPE ]] && HEADER+="($SCOPE)"
HEADER+="${BANG}: ${DESC}"

# Body (optional, multi-line — blank line ends input).
echo "body (optional; finish with empty line):" >&2
BODY=''
while IFS= read -r line; do
  [[ -z $line ]] && break
  BODY+="${line}"$'\n'
done

# Footers (optional, one per line).
echo "footers (optional; 'Token: value' or 'BREAKING CHANGE: ...'; empty to end):" >&2
FOOTERS=''
while IFS= read -r line; do
  [[ -z $line ]] && break
  FOOTERS+="${line}"$'\n'
done

# Force a BREAKING CHANGE footer when '!' is set but none provided.
if [[ -n $BANG && $FOOTERS != *'BREAKING CHANGE:'* ]]; then
  read -rp "BREAKING CHANGE: " BC
  [[ -n $BC ]] || die "BREAKING CHANGE description required when using '!'"
  FOOTERS+="BREAKING CHANGE: ${BC}"$'\n'
fi

# Assemble message.
TMP=$(mktemp -t commit-msg.XXXXXX)
trap 'rm -f "$TMP"' EXIT

{
  printf '%s\n' "$HEADER"
  if [[ -n $BODY || -n $FOOTERS ]]; then
    printf '\n'
    [[ -n $BODY ]] && printf '%s' "$BODY"
    if [[ -n $FOOTERS ]]; then
      [[ -n $BODY ]] && printf '\n'
      printf '%s' "$FOOTERS"
    fi
  fi
} > "$TMP"

# Validate before committing.
validate_file "$TMP"

echo >&2
echo "── commit message ─────────────────────────────────────────────" >&2
sed 's/^/  /' "$TMP" >&2
echo "───────────────────────────────────────────────────────────────" >&2

if (( DRY_RUN )); then
  cat "$TMP"
  exit 0
fi

# Commit.
git commit -F "$TMP" "${GIT_EXTRA_ARGS[@]}"

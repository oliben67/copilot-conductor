#!/usr/bin/env bats
# Tests for src/setup.sh.functions

FUNCTIONS_FILE="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/setup.sh.functions"

# ── Fixtures ──────────────────────────────────────────────────────────────────

setup() {
  # Source the library in a subshell-safe way: export so stubs can see them
  # shellcheck source=../src/setup.sh.functions
  source "$FUNCTIONS_FILE"

  # Scratch dir per test
  TEST_DIR="$(mktemp -d)"
  BASHRC="$TEST_DIR/.bashrc"
  CONDUCTOR_HOME="$TEST_DIR"
  HOME="$TEST_DIR"
  export BASHRC CONDUCTOR_HOME HOME
}

teardown() {
  rm -rf "$TEST_DIR"
}

prepare_fake_appimage_runtime() {
  # Fake AppImage extraction behavior used by show_key.
  cat > "$CONDUCTOR_HOME/con-pilot.AppImage" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--appimage-extract" ]]; then
  mkdir -p squashfs-root
  exit 0
fi
echo "unsupported invocation" >&2
exit 1
EOF
  chmod +x "$CONDUCTOR_HOME/con-pilot.AppImage"

  # Fake appimagetool captures embedded key + mode and writes a dummy output.
  mkdir -p "$CONDUCTOR_HOME/.tools"
  cat > "$CONDUCTOR_HOME/.tools/appimagetool-x86_64.AppImage" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
APPDIR_SRC="$1"
OUTPUT="$2"
cp "$APPDIR_SRC/key" "$CONDUCTOR_HOME/.embedded-key"
stat -c '%a' "$APPDIR_SRC/key" > "$CONDUCTOR_HOME/.embedded-key-mode"
printf '#!/usr/bin/env bash\nexit 0\n' > "$OUTPUT"
chmod +x "$OUTPUT"
EOF
  chmod +x "$CONDUCTOR_HOME/.tools/appimagetool-x86_64.AppImage"
}


# ── archive_line ──────────────────────────────────────────────────────────────

@test "archive_line: returns line number after __ARCHIVE__ marker" {
  local fake_script="$TEST_DIR/run.sh"
  # Line 1: source, Line 2: archive_line, Line 3: exit, Line 4: __ARCHIVE__, Line 5: data
  # archive_line should return 5
  printf 'source "%s"\narchive_line\nexit 0\n__ARCHIVE__\ndata\n' "$FUNCTIONS_FILE" > "$fake_script"
  run bash "$fake_script"
  [ "$status" -eq 0 ]
  [ "$output" -eq 5 ]
}

# ── extract_to ────────────────────────────────────────────────────────────────

@test "extract_to: creates destination directory and extracts files" {
  local dest="$TEST_DIR/extracted"
  # Build a minimal tar.gz with one file
  mkdir -p "$TEST_DIR/src/pkg"
  echo "hello" > "$TEST_DIR/src/pkg/file.txt"
  tar czf "$TEST_DIR/payload.tar.gz" -C "$TEST_DIR/src" pkg

  # Line 1: source, Line 2: extract_to, Line 3: exit 0, Line 4: __ARCHIVE__, Line 5+: payload
  local fake_script="$TEST_DIR/fake_installer.sh"
  printf 'source "%s"\nextract_to "%s"\nexit 0\n__ARCHIVE__\n' "$FUNCTIONS_FILE" "$dest" > "$fake_script"
  cat "$TEST_DIR/payload.tar.gz" >> "$fake_script"

  run bash "$fake_script"
  [ "$status" -eq 0 ]
  [ -d "$dest" ]
  [ -f "$dest/file.txt" ]
}

# ── persist_var ───────────────────────────────────────────────────────────────

@test "persist_var: appends new variable to bashrc" {
  touch "$BASHRC"
  run persist_var "CONDUCTOR_HOME" "/home/user/.conductor"
  [ "$status" -eq 0 ]
  grep -q 'export CONDUCTOR_HOME="/home/user/.conductor"' "$BASHRC"
}

@test "persist_var: updates existing variable in bashrc" {
  echo 'export CONDUCTOR_HOME="/old/path"' > "$BASHRC"
  run persist_var "CONDUCTOR_HOME" "/new/path"
  [ "$status" -eq 0 ]
  grep -q 'export CONDUCTOR_HOME="/new/path"' "$BASHRC"
  # Only one occurrence
  [ "$(grep -c 'CONDUCTOR_HOME' "$BASHRC")" -eq 1 ]
}

@test "persist_var: creates bashrc if it does not exist" {
  # BASHRC does not exist yet
  run persist_var "MY_VAR" "my_value"
  [ "$status" -eq 0 ]
  [ -f "$BASHRC" ]
  grep -q 'export MY_VAR="my_value"' "$BASHRC"
}

# ── stop_existing ─────────────────────────────────────────────────────────────

@test "stop_existing: does nothing when no con-pilot process running" {
  # pgrep will find nothing — stub it to always return 1
  pgrep() { return 1; }
  export -f pgrep
  run stop_existing
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "stop_existing: prints message when process is found" {
  pgrep() { return 0; }
  pkill() { return 0; }
  sleep() { return 0; }
  export -f pgrep pkill sleep
  run stop_existing
  [ "$status" -eq 0 ]
  [[ "$output" == *"Stopping"* ]]
}

# ── show_key ──────────────────────────────────────────────────────────────────

@test "show_key: creates key file with a non-empty UUID" {
  [[ "${CONDUCTOR_ENV:-}" == "-dev" ]] && skip "dev build (CONDUCTOR_ENV=-dev): show_key not used"
  prepare_fake_appimage_runtime

  # Stub uuidgen
  uuidgen() { echo "test-uuid-1234"; }
  export -f uuidgen

  run show_key
  [ "$status" -eq 0 ]

  [ -f "$TEST_DIR/.embedded-key" ]
  [ "$(cat "$TEST_DIR/.embedded-key")" = "test-uuid-1234" ]
  # Must never persist host-side key file.
  [ ! -e "$TEST_DIR/key" ]
}

@test "show_key: key file has mode 600" {
  [[ "${CONDUCTOR_ENV:-}" == "-dev" ]] && skip "dev build (CONDUCTOR_ENV=-dev): show_key not used"
  prepare_fake_appimage_runtime

  uuidgen() { echo "test-uuid-1234"; }
  export -f uuidgen

  show_key >/dev/null 2>&1
  [ -f "$TEST_DIR/.embedded-key-mode" ]
  [ "$(cat "$TEST_DIR/.embedded-key-mode")" = "600" ]
}

@test "show_key: prints ADMIN KEY to stdout" {
  [[ "${CONDUCTOR_ENV:-}" == "-dev" ]] && skip "dev build (CONDUCTOR_ENV=-dev): show_key not used"
  prepare_fake_appimage_runtime

  uuidgen() { echo "test-uuid-abcd"; }
  export -f uuidgen

  run show_key
  [ "$status" -eq 0 ]
  [[ "$output" == *"ADMIN KEY"* ]]
  [[ "$output" == *"test-uuid-abcd"* ]]
}

@test "show_key: falls back to /proc/sys/kernel/random/uuid when uuidgen missing" {
  [[ "${CONDUCTOR_ENV:-}" == "-dev" ]] && skip "dev build (CONDUCTOR_ENV=-dev): show_key not used"
  prepare_fake_appimage_runtime

  # Stub uuidgen to force fallback path (cat /proc/sys/kernel/random/uuid).
  uuidgen() { return 127; }
  export -f uuidgen

  run show_key
  [ "$status" -eq 0 ]
  [[ "$output" == *"ADMIN KEY"* ]]

  [ -f "$TEST_DIR/.embedded-key" ]
  [ -s "$TEST_DIR/.embedded-key" ]
}

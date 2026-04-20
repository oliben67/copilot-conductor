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
  APP_ID="io.test.App"
  HOME="$TEST_DIR"
  export BASHRC APP_ID HOME
}

teardown() {
  rm -rf "$TEST_DIR"
}

# ── require_flatpak ───────────────────────────────────────────────────────────

@test "require_flatpak: passes when flatpak is on PATH" {
  flatpak() { return 0; }
  export -f flatpak
  run require_flatpak
  [ "$status" -eq 0 ]
}

@test "require_flatpak: exits 1 with error when flatpak missing" {
  local empty_bin="$TEST_DIR/empty_bin"
  mkdir -p "$empty_bin"
  # Run in a subprocess so the stripped PATH doesn't affect teardown
  run bash -c "source '$FUNCTIONS_FILE'; PATH='$empty_bin'; require_flatpak"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Flatpak is not installed"* ]]
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
  # Stub uuidgen
  uuidgen() { echo "test-uuid-1234"; }
  export -f uuidgen

  run show_key
  [ "$status" -eq 0 ]

  local key_file="$TEST_DIR/.var/app/${APP_ID}/data/key"
  [ -f "$key_file" ]
  [ "$(cat "$key_file")" = "test-uuid-1234" ]
}

@test "show_key: key file has mode 600" {
  uuidgen() { echo "test-uuid-1234"; }
  export -f uuidgen

  show_key >/dev/null 2>&1
  local key_file="$TEST_DIR/.var/app/${APP_ID}/data/key"
  local perms
  perms="$(stat -c '%a' "$key_file")"
  [ "$perms" = "600" ]
}

@test "show_key: prints ADMIN KEY to stdout" {
  uuidgen() { echo "test-uuid-abcd"; }
  export -f uuidgen

  run show_key
  [ "$status" -eq 0 ]
  [[ "$output" == *"ADMIN KEY"* ]]
  [[ "$output" == *"test-uuid-abcd"* ]]
}

@test "show_key: falls back to /proc/sys/kernel/random/uuid when uuidgen missing" {
  local empty_bin="$TEST_DIR/empty_bin"
  mkdir -p "$empty_bin"
  # Run in subprocess with empty PATH to hide uuidgen; /proc/sys/kernel/random/uuid is always readable
  run bash -c "
    source '$FUNCTIONS_FILE'
    APP_ID='$APP_ID'
    HOME='$TEST_DIR'
    PATH='$empty_bin'
    show_key
  "
  [ "$status" -eq 0 ]
  [[ "$output" == *"ADMIN KEY"* ]]
}

#!/bin/sh
# Install or refresh the Android SDK, emulator and API 34 google_apis x86_64 system
# image into $ANDROID_SDK_ROOT (a shared Docker volume). Safe to run repeatedly.
set -eu
SDK="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
# Pinned command-line tools (the same package the Android Gradle plugin uses).
CMDLINE_URL="${TB_ANDROID_CMDLINE_URL:-https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip}"
PKG_EMU="emulator"
PKG_PLATFORM="platform-tools"
PKG_IMAGE="system-images;android-34;google_apis;x86_64"
LOCK="$SDK/.teambots-setup.lock"
MARKER="$SDK/.teambots-ready-api34"

mkdir -p "$SDK"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock 9
fi

if [ -f "$MARKER" ] && [ -x "$SDK/platform-tools/adb" ] && [ -x "$SDK/emulator/emulator" ] \
    && [ -d "$SDK/system-images/android-34/google_apis/x86_64" ]; then
  echo "Android SDK already prepared in $SDK"
  exit 0
fi

if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
  tmp="$(mktemp -d)"
  echo "Downloading Android command-line tools…"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$CMDLINE_URL" -o "$tmp/cmdline-tools.zip"
  else
    wget -q "$CMDLINE_URL" -O "$tmp/cmdline-tools.zip"
  fi
  unzip -q "$tmp/cmdline-tools.zip" -d "$tmp"
  mkdir -p "$SDK/cmdline-tools"
  rm -rf "$SDK/cmdline-tools/latest"
  # The zip contains a top-level "cmdline-tools" directory.
  if [ -d "$tmp/cmdline-tools" ]; then
    mv "$tmp/cmdline-tools" "$SDK/cmdline-tools/latest"
  else
    mv "$tmp"/* "$SDK/cmdline-tools/latest"
  fi
  rm -rf "$tmp"
fi

export ANDROID_SDK_ROOT="$SDK"
export ANDROID_HOME="$SDK"
export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$SDK/emulator:$PATH"

# Accept licences non-interactively (required by sdkmanager).
yes 2>/dev/null | sdkmanager --sdk_root="$SDK" --licenses >/dev/null || true
sdkmanager --sdk_root="$SDK" --install "$PKG_PLATFORM" "$PKG_EMU" "$PKG_IMAGE"

# Sanity checks so a half-installed volume is not marked ready.
command -v "$SDK/platform-tools/adb" >/dev/null
command -v "$SDK/emulator/emulator" >/dev/null
test -d "$SDK/system-images/android-34/google_apis/x86_64"
date -u +"ready %Y-%m-%dT%H:%M:%SZ" > "$MARKER"
echo "Android SDK ready in $SDK"

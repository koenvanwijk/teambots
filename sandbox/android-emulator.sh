#!/bin/sh
# Start the TeamBots AVD on the current DISPLAY (the bot's Xvfb). Do not use
# -no-window: the window must appear on the existing noVNC screen.
set -eu
SDK="${ANDROID_SDK_ROOT:-/opt/android-sdk}"
export ANDROID_SDK_ROOT="$SDK"
export ANDROID_HOME="$SDK"
export ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-/workspace/.android/avd}"
export ANDROID_EMULATOR_HOME="${ANDROID_EMULATOR_HOME:-/workspace/.android}"
export PATH="$SDK/platform-tools:$SDK/emulator:$SDK/cmdline-tools/latest/bin:$PATH"

if [ -z "${DISPLAY:-}" ]; then
  echo "DISPLAY is not set; the emulator needs the bot's virtual screen." >&2
  exit 1
fi

mkdir -p "$ANDROID_AVD_HOME" "$ANDROID_EMULATOR_HOME"
AVD="${TB_ANDROID_AVD:-teambots}"
# avdmanager writes $ANDROID_AVD_HOME/<name>.avd and an .ini next to it.
if [ ! -d "$ANDROID_AVD_HOME/${AVD}.avd" ]; then
  echo "Creating AVD $AVD (API 34 google_apis x86_64)…"
  echo no | avdmanager create avd -n "$AVD" \
    -k "system-images;android-34;google_apis;x86_64" \
    -d pixel_3a --force
fi

ACCEL="${TB_ANDROID_ACCEL:-on}"
MEM="${TB_ANDROID_GUEST_MEMORY:-2048}"
CORES="${TB_ANDROID_GUEST_CORES:-2}"
# Swiftshader works on Xvfb without a host GPU. -no-audio / -no-metrics keep
# the sandbox quiet. Skin 360x720 fits on the 1280x800 virtual screen.
exec emulator -avd "$AVD" \
  -gpu swiftshader_indirect \
  -no-audio \
  -no-boot-anim \
  -no-metrics \
  -accel "$ACCEL" \
  -memory "$MEM" \
  -cores "$CORES" \
  -skin 360x720

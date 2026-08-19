#!/bin/sh
set -eu

NODE_VERSION="${NODE_VERSION:-v22.23.2}"
NODE_ARCHIVE="node-${NODE_VERSION}-linux-armv7l"
APP_DIR="${APP_DIR:-/home/pi/pi_kiosk}"
PROVIDER_DIR="${PROVIDER_DIR:-/home/pi/bgutil-ytdlp-pot-provider}"
PROVIDER_VERSION="${PROVIDER_VERSION:-1.3.1}"

if [ "$(uname -m)" != "armv7l" ]; then
  echo "ERROR: This installer is intended for the Pi's armv7l OS." >&2
  exit 1
fi

if [ ! -x "$APP_DIR/venv/bin/python3" ]; then
  echo "ERROR: Python environment not found at $APP_DIR/venv" >&2
  exit 1
fi

echo "Installing system prerequisites..."
sudo apt-get update
sudo apt-get install -y \
  curl git xz-utils build-essential pkg-config \
  libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT INT TERM

echo "Installing Node.js $NODE_VERSION for ARMv7..."
curl -fL "https://nodejs.org/dist/${NODE_VERSION}/${NODE_ARCHIVE}.tar.xz" \
  -o "$tmp_dir/node.tar.xz"
sudo tar -xJf "$tmp_dir/node.tar.xz" -C /opt
sudo ln -sfn "/opt/${NODE_ARCHIVE}/bin/node" /usr/local/bin/node
sudo ln -sfn "/opt/${NODE_ARCHIVE}/bin/npm" /usr/local/bin/npm
sudo ln -sfn "/opt/${NODE_ARCHIVE}/bin/npx" /usr/local/bin/npx

echo "Updating yt-dlp and installing its PO token plugin..."
"$APP_DIR/venv/bin/python3" -m pip install -U \
  'yt-dlp[default]>=2026.7.4' \
  "bgutil-ytdlp-pot-provider==${PROVIDER_VERSION}"

if [ ! -d "$PROVIDER_DIR/.git" ]; then
  echo "Cloning PO token provider $PROVIDER_VERSION..."
  git clone --single-branch --branch "$PROVIDER_VERSION" \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    "$PROVIDER_DIR"
else
  installed_provider_version="$(git -C "$PROVIDER_DIR" describe --tags --exact-match 2>/dev/null || true)"
  if [ "$installed_provider_version" != "$PROVIDER_VERSION" ]; then
    echo "ERROR: $PROVIDER_DIR is not provider version $PROVIDER_VERSION." >&2
    echo "Move that directory aside and run this installer again." >&2
    exit 1
  fi
  echo "Using provider $installed_provider_version at $PROVIDER_DIR"
fi

echo "Building PO token provider..."
cd "$PROVIDER_DIR/server"
/usr/local/bin/npm ci
/usr/local/bin/npx tsc

echo
echo "YouTube support installed successfully."
echo "Node: $(/usr/local/bin/node --version)"
echo "yt-dlp: $("$APP_DIR/venv/bin/python3" -m yt_dlp --version)"
echo "Reboot the Pi to start the provider and kiosk: sudo reboot"

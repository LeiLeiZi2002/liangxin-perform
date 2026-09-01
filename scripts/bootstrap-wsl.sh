#!/usr/bin/env bash

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must be run as root." >&2
  echo "Example: wsl.exe -d Ubuntu-26.04 -u root -- bash scripts/bootstrap-wsl.sh" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

dpkg --configure -a
apt-get update
apt-get install -y --no-install-recommends \
  nodejs \
  npm \
  python3-pip \
  python3-venv

echo "Installed tool versions:"
python3 --version
python3 -m pip --version
node --version
npm --version

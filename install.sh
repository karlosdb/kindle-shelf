#!/usr/bin/env bash
# Links the command, installs the user service. A card UI, if you have one, is installed separately
# (see card_command in the config).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -sf "$here/bin/kindle-shelf" ~/.local/bin/kindle-shelf
ln -sf "$here/systemd/kindle-shelf.service" ~/.config/systemd/user/kindle-shelf.service
command -v ebook-convert >/dev/null || echo "Calibre is missing: omarchy-pkg-add calibre"
systemctl --user daemon-reload
systemctl --user enable --now kindle-shelf.service
systemctl --user restart kindle-shelf.service
echo "kindle-shelf installed"

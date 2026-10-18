#!/bin/bash
# Build the release tarball install.sh unpacks:
#   remora/{VERSION,remora-start,plugin_loader.service,move-plugins.py,site/}
# site/ is the backend and its pinned dependencies as pure-Python wheels, so one tarball serves
# x86_64 and aarch64 and any system Python from 3.11 up (SteamOS 3.11, Bazzite 3.13+).
# Usage: packaging/steamos/make-tarball.sh [OUT_DIR]   (default: dist/)
# PIP picks the pip to run (default "python3 -m pip"; "uvx pip" works too).
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
out=$(mkdir -p "${1:-$repo/dist}" && cd "${1:-$repo/dist}" && pwd)
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$repo/backend/remora/__init__.py")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

(cd "$repo/frontend" && npx --yes pnpm@10 install --frozen-lockfile --ignore-scripts && npx --yes pnpm@10 run build)

root=$work/remora
mkdir -p "$root/site"
# pip evaluates environment markers for the Python running it; the tarball must also work on
# 3.11, so drop the "< 3.13" marker and always ship typing-extensions
sed "s/ ; python_full_version < '3.13'//" "$repo/packaging/requirements.txt" >"$work/requirements.txt"
read -ra pip <<<"${PIP:-python3 -m pip}"
"${pip[@]}" install --no-cache-dir --no-compile --disable-pip-version-check \
  --require-hashes --no-deps --only-binary=:all: --implementation py --python-version 3.11 \
  --platform any --platform manylinux2014_x86_64 \
  --target "$root/site" -r "$work/requirements.txt"
rm -rf "$root/site/bin"
if find "$root/site" -name '*.so' | grep -q .; then
  echo "make-tarball: a compiled module got in; the tarball must be pure Python" >&2
  exit 1
fi
cp -r "$repo/backend/remora" "$root/site/"
# the compat list (store/compat.py) is package data next to the code
[ -f "$root/site/remora/data/compat.json" ] || { echo "make-tarball: no remora/data/compat.json" >&2; exit 1; }
find "$root/site" -name __pycache__ -prune -exec rm -rf {} +
install -m755 "$repo/packaging/remora-start" "$root/remora-start"
install -m644 "$repo/packaging/systemd/plugin_loader.service" "$root/plugin_loader.service"
install -m644 "$repo/packaging/steamos/move-plugins.py" "$root/move-plugins.py"
install -m644 "$repo/LICENSE" "$root/LICENSE"
echo "$version" >"$root/VERSION"

tarball=$out/remora-$version.tar.gz
tar -C "$work" --owner=0 --group=0 --numeric-owner -czf "$tarball" remora
cp "$tarball" "$out/remora.tar.gz"
(cd "$out" && sha256sum remora.tar.gz >remora.tar.gz.sha256 && sha256sum "remora-$version.tar.gz" >"remora-$version.tar.gz.sha256")
echo "$tarball"

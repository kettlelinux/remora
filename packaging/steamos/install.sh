#!/bin/bash
# Install (or update) Remora on SteamOS or Bazzite in place of Decky Loader.
#
#   curl -fLO https://github.com/kettlelinux/remora/releases/latest/download/install.sh
#   sudo bash install.sh                      # downloads the latest release
#   sudo bash install.sh --tarball remora.tar.gz
#
# - Remora goes in /home/.remora (or --prefix): root's, under /home so it survives SteamOS updates.
#   Not ~/homebrew/services like Decky: the user could swap a folder there that root then runs
# - Decky's plugin_loader.service is stopped and disabled, its unit file kept in
#   /etc/remora/plugin_loader.service.decky, and replaced by Remora's under the same name
#   (plugins restart the loader by that name)
# - ~/homebrew/plugins, settings, data and logs are not touched: Decky's plugins and their
#   settings carry over, and loader.json keeps its keys
# - running it again updates Remora in place; uninstall.sh puts Decky back
# Network: only to download the release tarball (and its .sha256), when --tarball isn't given.
set -euo pipefail

release_url=${REMORA_RELEASE_URL:-https://github.com/kettlelinux/remora/releases/latest/download/remora.tar.gz}
tarball='' sha256='' user='' prefix='' start=1

usage() {
  sed -n '2,16s/^# \{0,1\}//p' "$0"
  cat <<EOF
Options:
  --tarball FILE   install this release tarball instead of downloading one
  --sha256 HASH    check the tarball against this SHA-256
  --url URL        download from URL (default $release_url)
  --user NAME      the Game Mode user (default: the user running sudo, else uid 1000)
  --prefix DIR     where Remora goes (default /home/.remora)
  --no-start       install and enable, but don't (re)start the service
EOF
}

while [ $# -gt 0 ]; do
  case $1 in
    --tarball) tarball=$(readlink -f "$2"); shift 2 ;;
    --sha256) sha256=$2; shift 2 ;;
    --url) release_url=$2; shift 2 ;;
    --user) user=$2; shift 2 ;;
    --prefix) prefix=$2; shift 2 ;;
    --no-start) start=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done

say() { echo "remora: $*"; }
die() { echo "remora: $*" >&2; exit 1; }

if [ "$(id -u)" != 0 ]; then
  say "this needs root; running it again with sudo"
  args=()
  [ -n "$tarball" ] && args+=(--tarball "$tarball")
  [ -n "$sha256" ] && args+=(--sha256 "$sha256")
  [ -n "$prefix" ] && args+=(--prefix "$prefix")
  [ "$start" = 0 ] && args+=(--no-start)
  exec sudo REMORA_RELEASE_URL="$release_url" bash "$(readlink -f "$0")" --user "${user:-$(id -un)}" "${args[@]}"
fi

[ -n "$user" ] || user=${SUDO_USER:-}
[ -n "$user" ] && [ "$user" != root ] || user=$(getent passwd 1000 | cut -d: -f1)
IFS=: read -r user _ uid gid _ home _ < <(getent passwd "$user") || die "no user $user"
hb=$home/homebrew
prefix=${prefix:-/home/.remora}
case $prefix in /*) ;; *) die "--prefix must be an absolute path" ;; esac
unit=/etc/systemd/system/plugin_loader.service
conf=/etc/remora

py=/usr/bin/python3
"$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || die "Remora needs Python 3.11 or newer in $py"
command -v setpriv >/dev/null || die "setpriv (util-linux) is missing"

asuser() { setpriv --reuid="$uid" --regid="$gid" --clear-groups -- "$@"; }

work=$(mktemp -d -t remora-install.XXXXXX)
trap 'rm -rf "$work"' EXIT

# -- get and check the release -----------------------------------------------------------------
if [ -z "$tarball" ]; then
  say "downloading $release_url"
  curl -fL --retry 3 -o "$work/remora.tar.gz" "$release_url" || die "download failed"
  tarball=$work/remora.tar.gz
  if [ -z "$sha256" ] && curl -fsL --retry 3 -o "$work/remora.tar.gz.sha256" "$release_url.sha256"; then
    sha256=$(cut -d' ' -f1 "$work/remora.tar.gz.sha256")
  fi
  [ -n "$sha256" ] || say "warning: no checksum published next to the tarball; not checked"
fi
[ -f "$tarball" ] || die "no such file: $tarball"
if [ -n "$sha256" ]; then
  echo "$sha256  $tarball" | sha256sum -c --quiet - || die "checksum mismatch for $tarball"
fi
tar -C "$work" --no-same-owner -xzf "$tarball" remora || die "$tarball is not a Remora release"
new=$work/remora
for f in VERSION remora-start plugin_loader.service site/remora/__init__.py; do
  [ -e "$new/$f" ] || die "$tarball has no remora/$f"
done
version=$(cat "$new/VERSION")
PYTHONPATH=$new/site "$py" -P -c 'import remora.app' || die "this release doesn't import on $("$py" -V)"

# -- stop Decky (or an older Remora) -------------------------------------------------------------
ours() { [ -f "$1" ] && grep -q '^Description=Remora' "$1"; }
mkdir -p "$conf"
chmod 755 "$conf"
if systemctl list-unit-files plugin_loader.service >/dev/null 2>&1 \
   && systemctl cat plugin_loader.service >/dev/null 2>&1; then
  say "stopping plugin_loader.service"
  systemctl stop plugin_loader.service || :
  if ! ours "$unit"; then
    systemctl disable plugin_loader.service 2>/dev/null || :
  fi
fi
if [ -f "$unit" ] && ! ours "$unit" && [ ! -e "$conf/plugin_loader.service.decky" ]; then
  cp -p "$unit" "$conf/plugin_loader.service.decky"
  say "kept Decky's unit file as $conf/plugin_loader.service.decky"
fi

# -- install --------------------------------------------------------------------------------------
# folders in the user's home are made as the user; Remora's own files belong to root, so a
# plugin running as the user can't change the loader
asuser mkdir -p "$hb"/{plugins,settings,data,logs,services}
parent=$(dirname "$prefix")
[ -d "$parent" ] || asuser mkdir -p "$parent"
staging=$(mktemp -d "$parent/.remora-new.XXXXXX")
cp -r "$new/." "$staging/"
chown -R 0:0 "$staging"
chmod -R u=rwX,go=rX "$staging"
chmod 755 "$staging" "$staging/remora-start"
"$py" -m compileall -q -d "$prefix/site" "$staging/site" >/dev/null 2>&1 || :
if [ -e "$prefix" ]; then
  [ -f "$prefix/VERSION" ] && [ -f "$prefix/remora-start" ] \
    || die "$prefix exists and isn't a Remora install; pick another --prefix"
  mv -T "$prefix" "$prefix.old"
fi
mv -T "$staging" "$prefix"
rm -rf "$prefix.old"
if command -v selinuxenabled >/dev/null && selinuxenabled 2>/dev/null; then
  # Bazzite: a system service may only run files labelled for it, not user_home_t
  chcon -R -t bin_t "$prefix" || say "warning: chcon failed; the service may not start under SELinux"
fi

if [ ! -f "$conf/remora.env" ]; then
  cat >"$conf/remora.env" <<EOF
# Written by Remora's install.sh; read by $prefix/remora-start. See remora-start for the keys.
REMORA_USER=$user
UNPRIVILEGED_PATH=$hb
PRIVILEGED_PATH=$hb
EOF
fi
chown 0:0 "$conf/remora.env"
chmod 644 "$conf/remora.env"

sed "s|^ExecStart=.*|ExecStart=$prefix/remora-start|" "$new/plugin_loader.service" >"$work/unit"
install -m644 "$work/unit" "$unit"
systemctl daemon-reload
systemctl enable plugin_loader.service >/dev/null
if [ "$start" = 1 ]; then
  systemctl restart plugin_loader.service
  say "Remora $version installed and running. Restart Steam (or reboot) once if the Remora tab doesn't show up."
else
  say "Remora $version installed; it starts at the next boot (or: sudo systemctl start plugin_loader)"
fi

#!/bin/bash
# Install (or update) Remora on SteamOS or Bazzite in place of Decky Loader.
#
#   curl -fLO https://github.com/kettlelinux/remora/releases/latest/download/install.sh
#   sudo bash install.sh                      # downloads the latest release
#   sudo bash install.sh --tarball remora.tar.gz
#
# - Remora goes in /home/.remora (or --prefix): root's, under /home so it survives SteamOS updates.
#   Not ~/homebrew/services like Decky: the user could swap a folder there that root then runs.
#   Every folder above it must be root's and not writable by group or others
# - plugins/ and settings/loader.json move from ~/homebrew to /home/.remora-data (root's), so
#   the user (or a game) can't add a plugin that runs as root by writing files; each plugin's own
#   settings, data and logs stay in ~/homebrew
# - Decky's plugin_loader.service is stopped and disabled, its unit file kept in
#   /etc/remora/plugin_loader.service.decky, and replaced by Remora's under the same name
#   (plugins restart the loader by that name). Everything is staged and checked first; if
#   Remora then doesn't start, the old loader, its unit and its plugins are put back
# - running it again updates Remora in place; uninstall.sh puts Decky back
# - store sources with pinned signing keys need python's cryptography module; without it their
#   plugins are refused (unpinned and Decky sources work without it)
# Network: only to download the release tarball (and its .sha256), when --tarball isn't given.
set -euo pipefail

release_url=${REMORA_RELEASE_URL:-https://github.com/kettlelinux/remora/releases/latest/download/remora.tar.gz}
tarball='' sha256='' user='' prefix='' data='' start=1

usage() {
  sed -n '2,22s/^# \{0,1\}//p' "$0"
  cat <<EOF
Options:
  --tarball FILE   install this release tarball instead of downloading one
  --sha256 HASH    check the tarball against this SHA-256
  --url URL        download from URL (default $release_url)
  --user NAME      the Game Mode user (default: the user running sudo, else uid 1000)
  --prefix DIR     where Remora goes (default /home/.remora)
  --data DIR       plugins/ and settings/loader.json (default PREFIX-data); root's
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
    --data) data=$2; shift 2 ;;
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
  [ -n "$data" ] && args+=(--data "$data")
  [ "$start" = 0 ] && args+=(--no-start)
  exec sudo REMORA_RELEASE_URL="$release_url" bash "$(readlink -f "$0")" --user "${user:-$(id -un)}" "${args[@]}"
fi

[ -n "$user" ] || user=${SUDO_USER:-}
[ -n "$user" ] && [ "$user" != root ] || user=$(getent passwd 1000 | cut -d: -f1)
IFS=: read -r user _ uid gid _ home _ < <(getent passwd "$user") || die "no user $user"
hb=$home/homebrew
prefix=${prefix:-/home/.remora}
case $prefix in /*) ;; *) die "--prefix must be an absolute path" ;; esac
data=${data:-$prefix-data}
case $data in /*) ;; *) die "--data must be an absolute path" ;; esac
# resolved once (Bazzite's /home is a link to /var/home); only the resolved paths are used
prefix=$(realpath -m -- "$prefix")
data=$(realpath -m -- "$data")
[ "$prefix" != / ] && [ "$data" != / ] || die "--prefix and --data can't be /"
case $data/ in "$prefix"/*) die "--data can't be inside --prefix (updates replace the prefix)" ;; esac
unit=/etc/systemd/system/plugin_loader.service
conf=/etc/remora

# Every existing folder on the way to $1 (and $1 itself) must be root's, a real folder, and not
# writable by group or others: otherwise someone else could swap what root later runs
check_root_path() {
  local cur='' part owner mode
  local -a parts
  IFS=/ read -ra parts <<<"${1#/}"
  for part in '' "${parts[@]}"; do
    [ -n "$part" ] || [ -z "$cur" ] || continue
    cur=${cur%/}/$part
    [ -e "$cur" ] || [ -L "$cur" ] || return 0
    [ ! -L "$cur" ] || die "$cur is a symlink; pick another path"
    [ -d "$cur" ] || die "$cur is not a folder"
    read -r owner mode < <(stat -c '%u %a' -- "$cur")
    [ "$owner" = 0 ] || die "$cur doesn't belong to root; pick a path whose every folder is root's"
    (( (8#$mode & 8#022) == 0 )) || die "$cur is writable by group or others; pick another path"
  done
}
check_root_path "$prefix"
check_root_path "$data"

py=/usr/bin/python3
"$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || die "Remora needs Python 3.11 or newer in $py"
command -v setpriv >/dev/null || die "setpriv (util-linux) is missing"

asuser() { setpriv --reuid="$uid" --regid="$gid" --clear-groups -- "$@"; }

# -- state for putting things back --------------------------------------------------------------
work=$(mktemp -d -t remora-install.XXXXXX)
staging='' swapped=0 had_prefix=0 unit_touched=0 had_unit=0 env_touched=0 had_env=0
migrated=0 stopped=0 was_enabled=0 was_active=0 committed=0

rollback() {
  say "putting the previous loader back"
  [ -z "$staging" ] || rm -rf -- "$staging"
  if [ "$stopped" = 1 ] || [ "$unit_touched" = 1 ]; then
    systemctl stop plugin_loader.service 2>/dev/null || :
  fi
  if [ "$migrated" = 1 ]; then
    "$py" -I "$work/remora/move-plugins.py" out "$home" "$data" || say "warning: some plugins stay in $data/plugins"
  fi
  if [ "$swapped" = 1 ]; then
    rm -rf -- "$prefix"
    [ "$had_prefix" = 0 ] || mv -T -- "$prefix.old" "$prefix"
  fi
  if [ "$env_touched" = 1 ]; then
    if [ "$had_env" = 1 ]; then cp -p "$work/env.before" "$conf/remora.env"; else rm -f "$conf/remora.env"; fi
  fi
  if [ "$unit_touched" = 1 ]; then
    if [ "$had_unit" = 1 ]; then cp -p "$work/unit.before" "$unit"; else rm -f "$unit"; fi
  fi
  systemctl daemon-reload || :
  if [ "$stopped" = 1 ]; then
    [ "$was_enabled" = 0 ] || systemctl enable plugin_loader.service >/dev/null 2>&1 || :
    [ "$was_active" = 0 ] || systemctl start plugin_loader.service || say "warning: the old loader didn't start"
  fi
}
on_exit() {
  local status=$?
  trap - EXIT
  if [ "$committed" = 0 ] && [ "$status" != 0 ]; then rollback; fi
  rm -rf -- "$work"
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

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
for f in VERSION remora-start plugin_loader.service move-plugins.py site/remora/__init__.py; do
  [ -e "$new/$f" ] || die "$tarball has no remora/$f"
done
version=$(cat "$new/VERSION")
PYTHONPATH=$new/site "$py" -P -c 'import remora.app' || die "this release doesn't import on $("$py" -V)"
if ! "$py" -c 'import cryptography' 2>/dev/null; then
  say "note: python's cryptography module is missing: store sources with pinned signing keys will be refused"
fi
if [ -e "$prefix" ]; then
  [ -f "$prefix/VERSION" ] && [ -f "$prefix/remora-start" ] \
    || die "$prefix exists and isn't a Remora install; pick another --prefix"
fi

# -- stage the new one next to where it goes (nothing running is touched yet) ------------------
parent=$(dirname -- "$prefix")
install -d -m 755 -o 0 -g 0 -- "$parent"
install -d -m 755 -o 0 -g 0 -- "$(dirname -- "$data")"
check_root_path "$prefix"
check_root_path "$data"
staging=$(mktemp -d "$parent/.remora-new.XXXXXX")
cp -r "$new/." "$staging/"
chown -R 0:0 "$staging"
chmod -R u=rwX,go=rX "$staging"
chmod 755 "$staging" "$staging/remora-start"
"$py" -m compileall -q -d "$prefix/site" "$staging/site" >/dev/null 2>&1 || :
PYTHONPATH=$staging/site "$py" -P -c 'import remora.app' || die "the staged copy doesn't import"
sed "s|^ExecStart=.*|ExecStart=$prefix/remora-start|" "$new/plugin_loader.service" >"$work/unit"

# -- stop Decky (or an older Remora) -------------------------------------------------------------
ours() { [ -f "$1" ] && grep -q '^Description=Remora' "$1"; }
mkdir -p "$conf"
chmod 755 "$conf"
if systemctl cat plugin_loader.service >/dev/null 2>&1; then
  ! systemctl is-enabled --quiet plugin_loader.service 2>/dev/null || was_enabled=1
  ! systemctl is-active --quiet plugin_loader.service 2>/dev/null || was_active=1
  say "stopping plugin_loader.service"
  stopped=1
  systemctl stop plugin_loader.service || :
  if ! ours "$unit"; then
    systemctl disable plugin_loader.service 2>/dev/null || :
  fi
fi
if [ -f "$unit" ] && ! ours "$unit" && [ ! -e "$conf/plugin_loader.service.decky" ]; then
  cp -p "$unit" "$conf/plugin_loader.service.decky"
  say "kept Decky's unit file as $conf/plugin_loader.service.decky"
fi

# -- swap -------------------------------------------------------------------------------------------
# folders in the user's home are made as the user; Remora's own files belong to root, so a
# plugin running as the user can't change the loader
asuser mkdir -p "$hb"/{settings,data,logs,services}
rm -rf -- "$prefix.old"
if [ -e "$prefix" ]; then
  had_prefix=1
  mv -T -- "$prefix" "$prefix.old"
fi
swapped=1
mv -T -- "$staging" "$prefix"
staging=''
if command -v selinuxenabled >/dev/null && selinuxenabled 2>/dev/null; then
  # Bazzite: a system service may only run files labelled for it, not user_home_t. A file
  # context rule survives relabels; chcon only when semanage isn't there
  # shellcheck disable=SC2016  # a sed expression, not shell
  re=$(printf '%s' "$prefix" | sed 's/[][\.*^$()+?{}|]/\\&/g')
  if command -v semanage >/dev/null; then
    semanage fcontext -a -t bin_t "$re(/.*)?" 2>/dev/null || semanage fcontext -m -t bin_t "$re(/.*)?" \
      || say "warning: semanage failed"
    restorecon -R -- "$prefix" || say "warning: restorecon failed; the service may not start under SELinux"
  else
    chcon -R -t bin_t -- "$prefix" || say "warning: chcon failed; the service may not start under SELinux"
  fi
fi

install -d -m 755 -o 0 -g 0 -- "$data" "$data/plugins" "$data/settings"
if [ -f "$conf/remora.env" ]; then
  had_env=1
  cp -p "$conf/remora.env" "$work/env.before"
fi
if [ ! -f "$conf/remora.env" ]; then
  env_touched=1
  cat >"$conf/remora.env" <<EOF
# Written by Remora's install.sh; read by $prefix/remora-start. See remora-start for the keys.
REMORA_USER=$user
UNPRIVILEGED_PATH=$hb
PRIVILEGED_PATH=$data
EOF
elif grep -q "^# Written by Remora's install.sh" "$conf/remora.env" \
     && grep -qx "PRIVILEGED_PATH=$hb" "$conf/remora.env"; then
  # an older install.sh kept plugins in ~/homebrew; move them to root's folder
  env_touched=1
  sed -i "s|^PRIVILEGED_PATH=.*|PRIVILEGED_PATH=$data|" "$conf/remora.env"
  say "plugins/ and loader.json now live in $data"
fi
chown 0:0 "$conf/remora.env"
chmod 644 "$conf/remora.env"
priv=$(sed -n 's/^PRIVILEGED_PATH=//p' "$conf/remora.env" | tail -n1)

if [ -f "$unit" ]; then
  had_unit=1
  cp -p "$unit" "$work/unit.before"
fi
unit_touched=1
install -m644 "$work/unit" "$unit"
systemctl daemon-reload
systemctl enable plugin_loader.service >/dev/null

if [ "$priv" = "$data" ]; then
  migrated=1
  "$py" -I "$new/move-plugins.py" in "$home" "$data" \
    || say "warning: some entries stayed in $hb/plugins (Remora doesn't load them from there)"
fi

if [ "$start" = 1 ]; then
  systemctl restart plugin_loader.service
  sleep 3
  systemctl is-active --quiet plugin_loader.service \
    || die "Remora $version didn't start (journalctl -u plugin_loader)"
  committed=1
  rm -rf -- "$prefix.old"
  say "Remora $version installed and running. Restart Steam (or reboot) once if the Remora tab doesn't show up."
else
  committed=1
  rm -rf -- "$prefix.old"
  say "Remora $version installed; it starts at the next boot (or: sudo systemctl start plugin_loader)"
fi

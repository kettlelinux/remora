#!/bin/bash
# Remove Remora installed by install.sh, and put Decky Loader's service back if it was there.
#
#   sudo bash uninstall.sh [--user NAME] [--prefix DIR] [--purge-config]
#
# Plugins and loader.json move back from Remora's root-owned folder (PRIVILEGED_PATH in
# /etc/remora/remora.env) to ~/homebrew, so Decky (or a later Remora) finds the same plugins and
# settings; each plugin's settings, data and logs never left ~/homebrew. Keys Remora added to
# loader.json ("remora.*") stay; Decky ignores them. Running it twice is harmless.
set -euo pipefail

user='' prefix='' purge=0
while [ $# -gt 0 ]; do
  case $1 in
    --user) user=$2; shift 2 ;;
    --prefix) prefix=$2; shift 2 ;;
    --purge-config) purge=1; shift ;;
    -h|--help) sed -n '2,9s/^# \{0,1\}//p' "$0"; echo "  --purge-config  also remove /etc/remora (remora.env, the kept Decky unit)"; exit 0 ;;
    *) echo "uninstall.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

say() { echo "remora: $*"; }
die() { echo "remora: $*" >&2; exit 1; }

if [ "$(id -u)" != 0 ]; then
  args=(--user "${user:-$(id -un)}")
  [ -n "$prefix" ] && args+=(--prefix "$prefix")
  [ "$purge" = 1 ] && args+=(--purge-config)
  exec sudo bash "$(readlink -f "$0")" "${args[@]}"
fi

[ -n "$user" ] || user=${SUDO_USER:-}
[ -n "$user" ] && [ "$user" != root ] || user=$(getent passwd 1000 | cut -d: -f1)
IFS=: read -r user _ _ _ _ home _ < <(getent passwd "$user") || die "no user $user"
hb=$home/homebrew
prefix=${prefix:-/home/.remora}
unit=/etc/systemd/system/plugin_loader.service
conf=/etc/remora
backup=$conf/plugin_loader.service.decky

ours() { [ -f "$1" ] && grep -q '^Description=Remora' "$1"; }

if ours "$unit"; then
  say "stopping Remora"
  systemctl disable --now plugin_loader.service 2>/dev/null || :
  rm -f "$unit"
fi

priv=''
if [ -f "$conf/remora.env" ]; then
  priv=$(sed -n 's/^PRIVILEGED_PATH=//p' "$conf/remora.env" | tail -n1)
fi
if [ -n "$priv" ] && [ "$priv" != "$hb" ] && [ -d "$priv/plugins" ]; then
  if [ -f "$prefix/move-plugins.py" ]; then
    say "moving plugins and loader.json back to $hb"
    /usr/bin/python3 -I "$prefix/move-plugins.py" out "$home" "$priv" \
      || say "warning: some plugins stay in $priv/plugins"
    rmdir "$priv/plugins" "$priv/settings" "$priv" 2>/dev/null || :
  else
    say "plugins stay in $priv/plugins (no move-plugins.py in $prefix to move them back)"
  fi
fi

if [ -e "$prefix" ]; then
  if [ -f "$prefix/VERSION" ] && [ -f "$prefix/remora-start" ]; then
    rm -rf "$prefix"
    if command -v semanage >/dev/null && command -v selinuxenabled >/dev/null && selinuxenabled 2>/dev/null; then
      # shellcheck disable=SC2016  # a sed expression, not shell
      semanage fcontext -d "$(printf '%s' "$(realpath -m -- "$prefix")" | sed 's/[][\.*^$()+?{}|]/\\&/g')(/.*)?" 2>/dev/null || :
    fi
    say "removed $prefix"
  else
    say "$prefix doesn't look like a Remora install; left it alone"
  fi
fi

restored=0
if [ -f "$backup" ] && [ ! -e "$unit" ]; then
  exec_path=$(sed -n 's/^ExecStart=//p' "$backup" | head -n1 | cut -d' ' -f1)
  if [ -z "$exec_path" ] || [ -x "$exec_path" ]; then
    install -m644 "$backup" "$unit"
    restored=1
  else
    say "Decky's unit starts $exec_path, which is gone; not restoring it"
  fi
fi
systemctl daemon-reload
# a Decky unit shipped by the OS (/usr/lib/systemd/system) shows again once ours is gone
if [ "$restored" = 0 ] && systemctl cat plugin_loader.service >/dev/null 2>&1; then
  restored=1
fi
if [ "$restored" = 1 ]; then
  systemctl enable --now plugin_loader.service >/dev/null 2>&1 || say "warning: Decky's service didn't start"
  rm -f "$backup"
  say "Decky Loader's service is back"
else
  say "Remora is removed. To get Decky Loader back, run its installer; your plugins are still in $hb/plugins"
fi

if [ "$purge" = 1 ]; then
  rm -rf "$conf"
fi

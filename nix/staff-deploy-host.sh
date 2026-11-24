#!/usr/bin/env bash
# staff-deploy-host: the forced command of the staff-deploy user.
#
# The only caller is the door on suzuka (ARCHITECTURE.md, "On each host").
# The request is the one line in SSH_ORIGINAL_COMMAND:
#
#   status
#   dry-activate <rev> [--night-ok]  flake source as a tar archive on stdin
#   switch <rev> [--night-ok]     flake source as a tar archive on stdin
#   confirm <rev>
#   restart <unit> [--night-ok]
#   reboot [--night-ok]
#
# Anything else is refused. Nothing from the request is ever evaluated; it is
# matched against fixed patterns and passed on as separate arguments.
set -euo pipefail
umask 077

readonly SRC_ROOT=/var/lib/staff-deploy/src
readonly REV='[0-9a-f]{40}'
readonly UNIT='[A-Za-z0-9@_.:][A-Za-z0-9@_.:-]*\.(service|timer|socket)'

die() {
  echo "staff-deploy-host: $*" >&2
  exit 2
}

# Unpack the tar archive on stdin into a fresh $SRC_ROOT/<rev>.
unpack() {
  local rev=$1 dest=$SRC_ROOT/$1 tarball name kinds
  # Old sources are of no use once their switch is long over.
  find "$SRC_ROOT" -mindepth 1 -maxdepth 1 -mtime +7 -exec rm -rf -- {} +
  tarball=$(mktemp "$SRC_ROOT/.incoming.XXXXXX")
  # shellcheck disable=SC2064  # expand now: the path is fixed from here on
  trap "rm -f -- '$tarball'" EXIT
  cat >"$tarball"
  # No absolute paths and no '..' anywhere in a member name.
  while IFS= read -r name; do
    case "/$name/" in
      //* | */../*) die "archive has an unsafe path" ;;
    esac
  done < <(tar -tf "$tarball")
  # Only plain files, directories, and symlinks.
  kinds=$(tar -tvf "$tarball" | cut -c1 | sort -u | tr -d '\n')
  [[ $kinds =~ ^[-dl]+$ ]] || die "archive is empty or has an unsupported member type"
  rm -rf -- "$dest"
  mkdir -- "$dest"
  tar -xf "$tarball" -C "$dest" --no-same-owner --no-same-permissions
  [[ -f $dest/flake.nix && ! -L $dest/flake.nix ]] || die "archive has no flake.nix"
  rm -f -- "$tarball"
  trap - EXIT # exec below would skip it
}

cmd=${SSH_ORIGINAL_COMMAND:-}
[[ ${#cmd} -le 300 ]] || die "request too long"
allowed='^[A-Za-z0-9@_.: -]+$'
[[ $cmd =~ $allowed ]] || die "request refused"
re_dry="^dry-activate ($REV)( --night-ok)?\$"
re_switch="^switch ($REV)( --night-ok)?\$"
re_confirm="^confirm ($REV)\$"
re_restart="^restart ($UNIT)( --night-ok)?\$"
re_reboot='^reboot( --night-ok)?$'

if [[ $cmd == status ]]; then
  exec sudo -n staff-deploy-root status
elif [[ $cmd =~ $re_dry ]]; then
  rev=${BASH_REMATCH[1]}
  night=${BASH_REMATCH[2]# }
  unpack "$rev"
  exec sudo -n staff-deploy-root dry-activate "$rev" ${night:+"$night"}
elif [[ $cmd =~ $re_switch ]]; then
  rev=${BASH_REMATCH[1]}
  night=${BASH_REMATCH[2]# }
  unpack "$rev"
  exec sudo -n staff-deploy-root switch "$rev" ${night:+"$night"}
elif [[ $cmd =~ $re_confirm ]]; then
  exec sudo -n staff-deploy-root confirm "${BASH_REMATCH[1]}"
elif [[ $cmd =~ $re_restart ]]; then
  # The unit pattern has its own group, so the flag is the third.
  night=${BASH_REMATCH[3]# }
  exec sudo -n staff-deploy-root restart "${BASH_REMATCH[1]}" ${night:+"$night"}
elif [[ $cmd =~ $re_reboot ]]; then
  night=${BASH_REMATCH[1]# }
  exec sudo -n staff-deploy-root reboot ${night:+"$night"}
else
  die "request refused"
fi

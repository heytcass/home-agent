#!/usr/bin/env bash
# staff-deploy-root: the root side of the staff deploy path.
#
# Run only through the one sudo rule of the staff-deploy user, by
# staff-deploy-host. It trusts nothing it is given and checks every argument
# again.
#
#   status
#   dry-activate <rev> [--night-ok]
#   switch <rev> [--night-ok]
#   confirm <rev>
#   restart <unit> [--night-ok]
#   reboot [--night-ok]
#
# Two more verbs are only ever called by the transient units this script
# starts: `run-switch <rev>` (build, then activate) and `revert-now`.
#
# A switch builds first, then arms a revert timer, then activates the new
# system without making it the boot default. Only `confirm` moves the system
# profile and the boot default. So unless `confirm` arrives in time the host
# goes back to the system it was running before, and a host that reboots or
# crashes unconfirmed comes up on the old system with nothing to undo.
set -euo pipefail
umask 022

readonly STATE=/var/lib/staff-deploy
readonly RUN=/run/staff-deploy # gone after a reboot, like the revert timer
readonly LOGS=/var/log/fleet-deploy
readonly CONF=/etc/staff-deploy.conf
readonly CURRENT=/run/current-system
readonly PROFILE=/nix/var/nix/profiles/system
readonly REV='^[0-9a-f]{40}$'
readonly UNIT='^[A-Za-z0-9@_.:][A-Za-z0-9@_.:-]*\.(service|timer|socket)$'
readonly NIGHT_STRICT_HOST=monza

die() {
  echo "staff-deploy-root: $*" >&2
  exit 2
}

# conf_int KEY DEFAULT MAX: a whole number from $CONF, read as text, never sourced.
conf_int() {
  local key=$1 val=$2 max=$3 k v
  if [[ -r $CONF ]]; then
    while IFS='=' read -r k v || [[ -n $k ]]; do
      if [[ $k == "$key" && $v =~ ^[0-9]{1,6}$ ]] && ((10#$v <= max)); then
        val=$((10#$v))
      fi
    done <"$CONF"
  fi
  echo "$val"
}

# get NAME: the content of a state file, or nothing.
get() {
  if [[ -f $STATE/$1 ]]; then
    cat -- "$STATE/$1"
  fi
}

put() {
  echo "$2" >"$STATE/$1.tmp"
  mv -f -- "$STATE/$1.tmp" "$STATE/$1"
}

# The revert is armed for this rev, or nothing.
pending() {
  if [[ -f $RUN/pending ]]; then
    cat -- "$RUN/pending"
  fi
}

# A system is a real top-level store path, written exactly as it resolves.
is_system() {
  local re='^/nix/store/[a-z0-9]{32}-[A-Za-z0-9+._?=-]+$'
  [[ $1 =~ $re && $1 != *..* && -x $1/bin/switch-to-configuration ]] \
    && [[ $(readlink -f -- "$1") == "$1" ]]
}

# Units the staff may never restart: the deploy path, the staff itself, the
# way in, and anything that takes the host down.
denied_unit() {
  local re='reboot|poweroff|halt|kexec|shutdown'
  [[ $1 == @(staff-deploy-|systemd-|sshd|ssh.|ssh@|home-staff-|dbus)* || $1 =~ $re ]]
}

is_night() {
  local h s e
  h=$(date +%-H)
  s=$(conf_int NIGHT_START_HOUR 0 23)
  e=$(conf_int NIGHT_END_HOUR 7 23)
  if ((s == e)); then
    return 1
  elif ((s < e)); then
    ((h >= s && h < e))
  else
    ((h >= s || h < e))
  fi
}

# The door applies the night rule first. This is the second lock, on the one
# host where waking someone is possible.
night_gate() {
  if [[ $host == "$NIGHT_STRICT_HOST" && $night_ok != yes ]] && is_night; then
    die "refused: it is night on $host"
  fi
}

# none, building, activating, succeeded, failed, or lost.
switch_state() {
  local result
  result=$(get result)
  case $result in
    building | activating)
      if ! systemctl is-active --quiet staff-deploy-switch.service; then
        # Read again: the unit may have finished between the two looks.
        result=$(get result)
        if [[ $result == building || $result == activating ]]; then
          result=failed # the unit died without recording a result
        fi
      fi
      ;;
    succeeded)
      # Activated but never confirmed, and no revert armed: the host
      # rebooted in between and is back on its old system.
      if [[ -z $(pending) && $(get confirmed) != "$(get rev)" ]]; then
        result=lost
      fi
      ;;
  esac
  echo "${result:-none}"
}

in_flight() {
  local state
  state=$(switch_state)
  [[ -n $(pending) || $state == building || $state == activating ]]
}

source_of() {
  local src=$STATE/src/$1
  [[ -f $src/flake.nix && ! -L $src/flake.nix ]] || die "no source for $1"
  echo "$src"
}

# Transient units do not inherit this script's PATH.
start_unit() {
  systemd-run --quiet --setenv=PATH="$PATH" "$@"
}

do_status() {
  local current rev srev="" spath="" active="" armed=no running name names=()
  current=$(readlink -f "$CURRENT")
  rev=$(get rev)
  read -r srev spath < <(get switched) || true
  if [[ -n $srev && $spath == "$current" ]]; then
    active=$srev
  fi
  if [[ -n $(pending) ]]; then
    armed=yes
  fi
  running=$(systemctl is-system-running) || true
  while read -r name _; do
    if [[ -n $name ]]; then
      names+=("$name")
    fi
  done < <(systemctl list-units --state=failed --no-legend --plain | LC_ALL=C sort)
  echo "current_system=$current"
  echo "rev=$rev"
  echo "active_rev=$active"
  echo "switch=$(switch_state)"
  echo "revert_pending=$armed"
  echo "system_running=$running"
  echo "failed_units=${#names[@]}"
  echo "log=$(get log)"
  echo "failed_list=${names[*]:0:50}"
}

# Not harmless: a dry activation runs the new system's activation script, in
# dry mode, as root. So it is gated like a switch.
do_dry_activate() {
  local rev=$1 src log
  night_gate
  src=$(source_of "$rev")
  log=$LOGS/staff-$(date +%Y%m%dT%H%M%S)-$rev-dry.log
  nixos-rebuild dry-activate --flake "$src#$host" 2>&1 | tee -- "$log"
}

do_switch() {
  local rev=$1 previous revert_s log
  night_gate
  source_of "$rev" >/dev/null
  if in_flight || systemctl is-active --quiet staff-deploy-revert.service; then
    die "an earlier switch is not finished"
  fi
  previous=$(readlink -f "$CURRENT")
  is_system "$previous" || die "cannot tell what the current system is"
  revert_s=$(conf_int REVERT_AFTER_S 300 86400)
  ((revert_s >= 30)) || die "REVERT_AFTER_S is too short"
  log=$LOGS/staff-$(date +%Y%m%dT%H%M%S)-$rev.log

  # Leftovers of an earlier run would block the unit names.
  systemctl stop staff-deploy-revert.timer 2>/dev/null || true
  systemctl reset-failed staff-deploy-revert.service staff-deploy-switch.service 2>/dev/null || true

  put previous "$previous"
  put previous_profile "$(readlink -f "$PROFILE")"
  put rev "$rev"
  put log "$log"
  put result building
  rm -f -- "$STATE/switched"
  echo "switch to $rev from $previous" >"$log"

  start_unit --unit staff-deploy-switch -- "$self" run-switch "$rev"
  echo "switch started rev=$rev revert_after_s=$revert_s log=$log"
}

# Inside staff-deploy-switch.service: build, arm the revert, activate.
do_run_switch() {
  local rev=$1 src log build_s revert_s system
  src=$(source_of "$rev")
  log=$(get log)
  [[ $(get rev) == "$rev" && $(get result) == building && -n $log ]] \
    || die "no switch was started for $rev"
  build_s=$(conf_int BUILD_TIMEOUT_S 3600 86400)
  revert_s=$(conf_int REVERT_AFTER_S 300 86400)

  # Nothing on the host changes during the build, so no revert is armed yet.
  # build/result also keeps the new system from being collected.
  rm -rf -- "$STATE/build"
  mkdir -- "$STATE/build"
  cd -- "$STATE/build"
  if ! timeout "$build_s" nixos-rebuild build --flake "$src#$host" >>"$log" 2>&1; then
    echo "the build failed or took longer than ${build_s}s" >>"$log"
    put result failed
    exit 1
  fi
  system=$(readlink -f result)
  if ! is_system "$system"; then
    echo "the build left no system behind" >>"$log"
    put result failed
    exit 1
  fi

  # The revert is armed before anything can change.
  start_unit --unit staff-deploy-revert --on-active="$revert_s" \
    --timer-property=AccuracySec=1s -- "$self" revert-now
  echo "$rev" >"$RUN/pending"
  put result activating
  echo "activating $system, revert in ${revert_s}s unless confirmed" >>"$log"
  # `test`: the running system changes, the boot default does not.
  if "$system/bin/switch-to-configuration" test >>"$log" 2>&1; then
    put switched "$rev $system"
    put result succeeded
  else
    echo "activation failed" >>"$log"
    systemctl stop staff-deploy-revert.timer >>"$log" 2>&1 || true
    revert >>"$log" 2>&1
    exit 1
  fi
}

# Back to the recorded system. The profile only needs restoring if a confirm
# got halfway.
revert() {
  local previous previous_profile
  previous=$(get previous)
  previous_profile=$(get previous_profile)
  is_system "$previous" || die "no previous system recorded"
  rm -f -- "$RUN/pending" "$STATE/switched"
  put result failed
  echo "revert to $previous at $(date +%Y%m%dT%H%M%S)"
  if is_system "$previous_profile" && [[ $(readlink -f "$PROFILE") != "$previous_profile" ]]; then
    if ! { nix-env --profile "$PROFILE" --set "$previous_profile" \
      && "$previous_profile/bin/switch-to-configuration" boot; }; then
      echo "could not restore the boot default"
    fi
  fi
  if "$previous/bin/switch-to-configuration" test; then
    echo "reverted"
  else
    echo "REVERT FAILED: $host may still be running the new system"
    return 1
  fi
}

# Inside staff-deploy-revert.service, when the timer fires.
do_revert_now() {
  local log
  log=$(get log)
  [[ -n $(pending) ]] || die "nothing to revert"
  {
    # An activation that is still running must not finish after the revert.
    systemctl stop staff-deploy-switch.service || true
    revert
  } >>"${log:-/dev/null}" 2>&1
}

do_confirm() {
  local rev=$1 srev="" system="" log state
  log=$(get log)
  log=${log:-/dev/null}
  [[ $(pending) == "$rev" ]] || die "no revert is pending for $rev"
  [[ $(switch_state) == succeeded ]] || die "the switch to $rev has not succeeded"
  read -r srev system < <(get switched) || true
  if [[ $srev != "$rev" || $system != "$(readlink -f "$CURRENT")" ]] || ! is_system "$system"; then
    die "the running system is not the one built from $rev"
  fi
  # Stop the timer first, then look: if the revert got going before the stop,
  # it wins and nothing here touches the profile or the boot default.
  systemctl stop staff-deploy-revert.timer
  if ! state=$(systemctl show --property=ActiveState --value staff-deploy-revert.service); then
    give_up "could not tell whether the revert had started" "$log"
  fi
  if [[ ${state:-inactive} != inactive || $(pending) != "$rev" || $(switch_state) != succeeded ]]; then
    die "the revert has already started; $rev is not confirmed"
  fi
  # From here on this script owns the outcome: keep the system, or revert now.
  if nix-env --profile "$PROFILE" --set "$system" >>"$log" 2>&1 \
    && "$system/bin/switch-to-configuration" boot >>"$log" 2>&1; then
    put confirmed "$rev"
    rm -f -- "$RUN/pending"
    echo "confirmed" >>"$log"
    echo "confirmed rev=$rev"
  else
    give_up "could not make $rev the boot default" "$log"
  fi
}

# give_up WHY LOG: a confirm that cannot finish reverts at once and says how that went.
give_up() {
  if revert >>"$2" 2>&1; then
    die "$1; reverted"
  fi
  die "$1; THE REVERT FAILED, the host may still be on the new system"
}

do_restart() {
  local unit=$1 name names list
  night_gate
  if denied_unit "$unit"; then
    die "refused: $unit is not for the staff to restart"
  fi
  # The same rule for every name the unit goes by, so an alias is no way around it.
  names=$(systemctl show --property=Id --property=Names --value -- "$unit") \
    || die "cannot look up $unit"
  read -rd '' -a list <<<"$names" || true
  for name in "${list[@]}"; do
    if denied_unit "$name"; then
      die "refused: $unit is also $name, which is not for the staff to restart"
    fi
  done
  systemctl restart -- "$unit"
  echo "restarted $unit: $(systemctl is-active -- "$unit" || true)"
}

do_reboot() {
  night_gate
  if in_flight; then
    die "refused: a switch is not finished"
  fi
  # A moment later, so this reply still reaches the door.
  start_unit --on-active=5 -- systemctl reboot
  echo "reboot in 5 s"
}

self=$(readlink -f -- "$0")
host=$(hostname)
[[ $host =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "unexpected hostname"

args=("$@")
night_ok=no
if ((${#args[@]} > 1)) && [[ ${args[-1]} == --night-ok ]]; then
  night_ok=yes
  unset 'args[-1]'
fi
verb=${args[0]:-}
arg=${args[1]:-}

case "$verb:${#args[@]}" in
  status:1 | reboot:1 | revert-now:1) ;;
  dry-activate:2 | switch:2 | confirm:2 | run-switch:2)
    [[ $arg =~ $REV ]] || die "bad revision"
    ;;
  restart:2)
    [[ ${#arg} -le 200 && $arg =~ $UNIT ]] || die "bad unit name"
    ;;
  *) die "request refused" ;;
esac
if [[ $night_ok == yes && $verb != @(dry-activate|switch|restart|reboot) ]]; then
  die "request refused"
fi

mkdir -p -- "$STATE" "$LOGS" "$RUN"

case $verb in
  status) do_status ;;
  dry-activate) do_dry_activate "$arg" ;;
  switch) do_switch "$arg" ;;
  run-switch) do_run_switch "$arg" ;;
  revert-now) do_revert_now ;;
  confirm) do_confirm "$arg" ;;
  restart) do_restart "$arg" ;;
  reboot) do_reboot ;;
esac

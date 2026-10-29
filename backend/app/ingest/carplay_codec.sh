#!/system/bin/sh
# One bounded active-CarPlay capture, including driving. The caller owns gating.
# Usage: sh carplay_codec.sh SESSION SEQUENCE LOGFILE [LOGCAT_PRIORITY]
# Raw trace passes through a FIFO into awk; only numeric summaries touch disk.
SESSION=$1; SEQUENCE=$2; LOGFILE=$3; PRIO=${4:-w}
case "$SESSION" in ''|*[!A-Za-z0-9_.-]*) exit 2;; esac
case "$SEQUENCE" in ''|*[!0-9]*) exit 2;; esac
[ -n "$LOGFILE" ] || exit 2
case "$PRIO" in v|d|i|w|e) ;; *) PRIO=w;; esac
LOCK=/data/local/tmp/.dashcam_cpt_codec_capture.lock
GUARD=/data/local/tmp/.dashcam_cpt_codec_capture.guard
LEGACY_SCRIPT=/data/local/tmp/dashcam_carplay_codec.sh
umask 077
LC_ALL=C; export LC_ALL
SELFSTART=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$$/stat" 2>/dev/null)
TOKEN="$$:${SELFSTART:-unknown}:$SESSION:$SEQUENCE"
PIPE="$LOCK/pipe-$$-${SELFSTART:-unknown}"
SUMMARYFILE="$LOCK/summary-$$-${SELFSTART:-unknown}"
STATUS=unavailable; ATRACERC=na; OWNED=0; STARTED=0; WATCHDOGFIRED=0
LOCKSTATUS=not_attempted; GUARDED=0; CREATED=0; LOCKID=; GUARDID=
ATRACEPID=; AWKPID=; WATCHPID=; TRACEPATH=; SUMMARY=
clock_ms() { awk '{printf "%.0f",$1*1000}' /proc/uptime 2>/dev/null; }
STARTMS=$(clock_ms); STARTMS=${STARTMS:-0}
read_state() {
  STATE=na; TRACER=na
  [ -n "$TRACEPATH" ] || return
  read STATE < "$TRACEPATH/tracing_on" 2>/dev/null
  read TRACER < "$TRACEPATH/current_tracer" 2>/dev/null
  case "$STATE" in 0|1) ;; *) STATE=na;; esac
}
owns_lock() {
  [ "$OWNED" = 1 ] || return 1
  owns_workspace || return 1
  read owner < "$LOCK/owner" 2>/dev/null
  [ "$owner" = "$TOKEN" ]
}
owns_workspace() {
  [ "$CREATED" = 1 ] && [ "$GUARDED" = 1 ] && [ ! -L "$LOCK" ] &&
    [ "$(stat -c '%d:%i' "$LOCK" 2>/dev/null)" = "$LOCKID" ] &&
    [ ! -L "$GUARD" ] && [ "$(stat -c '%d:%i:%u:%a' "$GUARD" 2>/dev/null)" = "$GUARDID" ]
}
safe_members() {
  member_count=0
  for member in "$LOCK"/* "$LOCK"/.[!.]* "$LOCK"/..?*; do
    [ -e "$member" ] || [ -L "$member" ] || continue
    member_count=$((member_count+1)); [ "$member_count" -le 16 ] || return 1
    [ ! -L "$member" ] && [ "$(stat -c '%u:%h' "$member" 2>/dev/null)" = "$CAPTUREUID:1" ] || return 1
    case "$member" in
      "$LOCK/owner"|"$LOCK/summary") [ -f "$member" ] || return 1;;
      "$LOCK/pipe") [ -p "$member" ] || return 1;;
      "$LOCK/pipe-"*|"$LOCK/summary-"*)
        suffix=${member##*/}; suffix=${suffix#*-}
        member_pid=${suffix%%-*}; member_start=${suffix#*-}
        [ "$suffix" = "$member_pid-$member_start" ] || return 1
        case "$member_pid" in ''|*[!0-9]*) return 1;; esac
        case "$member_start" in ''|*[!0-9]*) return 1;; esac
        case "$member" in "$LOCK/pipe-"*) [ -p "$member" ] || return 1;; *) [ -f "$member" ] || return 1;; esac;;
      *) return 1;;
    esac
  done
}
remove_members() {
  safe_members || return 1
  for member in "$LOCK"/* "$LOCK"/.[!.]* "$LOCK"/..?*; do
    [ -e "$member" ] || [ -L "$member" ] || continue
    rm -f "$member" 8<&8 || return 1
  done
}
scan_clock() {
  read uptime_value ignored < /proc/uptime || return 1
  seconds=${uptime_value%%.*}; fraction=${uptime_value#*.}000
  fraction=${fraction%${fraction#???}}
  case "$seconds:$fraction" in ''|*[!0-9:]*) return 1;; esac
  [ -n "$seconds" ] && [ "${#fraction}" = 3 ] || return 1
  SCAN_SECONDS=$seconds; SCAN_MILLIS=$((1$fraction-1000))
}
scan_budget() {
  scan_clock || return 1
  scan_elapsed=$(((SCAN_SECONDS-scan_start_seconds)*1000+SCAN_MILLIS-scan_start_millis))
  if [ "$scan_elapsed" -lt 0 ] || [ "$scan_elapsed" -gt 4000 ]; then
    LEGACYSTATUS=legacy_scan_timeout; return 1
  fi
}
read_process_uid() {
  PROCESS_UID=
  while read -r field real effective saved filesystem ignored; do
    if [ "$field" = Uid: ]; then
      case "$real:$effective:$saved:$filesystem" in ''|*[!0-9:]*) return 1;; esac
      [ -n "$real" ] && [ -n "$effective" ] && [ -n "$saved" ] && [ -n "$filesystem" ] && [ -z "$ignored" ] || return 1
      PROCESS_UID=$effective; return 0
    fi
  done < "$1/status" 2>/dev/null
  return 1
}
read_process_identity() {
  IFS= read -r process_stat < "$1/stat" 2>/dev/null || return 1
  case "$process_stat" in *') '*) process_fields=${process_stat##*) };; *) return 1;; esac
  set -- $process_fields
  [ "$#" -ge 20 ] || return 1
  PROCESS_STATE=$1
  case "$PROCESS_STATE" in [A-Za-z]) ;; *) return 1;; esac
  shift 19; PROCESS_START=$1
  case "$PROCESS_START" in ''|*[!0-9]*) return 1;; esac
}
read_process_arguments() {
  PROCESS_ARGUMENTS=$({ head -c 65537 "$1/cmdline" 2>/dev/null || printf '\000codec_read_failed\000'; printf '\000codec_read_end'; } | tr '\000' '\n') || return 1
  case "$PROCESS_ARGUMENTS" in *codec_read_failed*) return 1;; esac
  case "$PROCESS_ARGUMENTS" in *"
codec_read_end") PROCESS_ARGUMENTS=${PROCESS_ARGUMENTS%"
codec_read_end"};; *) return 1;; esac
  [ "${#PROCESS_ARGUMENTS}" -le 65536 ]
}
legacy_helpers_absent() {
  # Only stale-workspace recovery needs this proof. Enumerating unrelated Android
  # tasks uses builtins, not a fork per PID; argv work is bounded to our live UID.
  LEGACYSTATUS=legacy_owner_unknown
  [ -r /proc/1/stat ] || return 1
  scan_clock || return 1
  scan_start_seconds=$SCAN_SECONDS; scan_start_millis=$SCAN_MILLIS; scanned=0; candidates=0
  for process in /proc/[0-9]*; do
    [ "$process" != "/proc/$$" ] || continue
    scanned=$((scanned+1)); [ "$scanned" -le 4096 ] || { LEGACYSTATUS=legacy_scan_limit; return 1; }
    scan_budget || return 1
    if ! read_process_uid "$process"; then [ ! -d "$process" ] && continue; return 1; fi
    [ "$PROCESS_UID" = "$CAPTUREUID" ] || continue
    if ! read_process_identity "$process"; then [ ! -d "$process" ] && continue; return 1; fi
    case "$PROCESS_STATE" in Z|X) continue;; esac
    candidates=$((candidates+1)); [ "$candidates" -le 128 ] || { LEGACYSTATUS=legacy_candidate_limit; return 1; }
    process_start=$PROCESS_START
    comm=
    if ! IFS= read -r comm < "$process/comm" 2>/dev/null; then
      [ ! -d "$process" ] && continue; return 1
    fi
    # Inspect every same-UID argv: an unrelated comm is not an exec-stability proof.
    if ! read_process_arguments "$process"; then [ ! -d "$process" ] && continue; return 1; fi
    arguments=$PROCESS_ARGUMENTS
    if ! read_process_arguments "$process"; then [ ! -d "$process" ] && continue; return 1; fi
    [ "$arguments" = "$PROCESS_ARGUMENTS" ] || return 1
    if ! read_process_identity "$process"; then [ ! -d "$process" ] && continue; return 1; fi
    case "$PROCESS_STATE" in Z|X) continue;; esac
    [ "$PROCESS_START" = "$process_start" ] || return 1
    actual_comm=
    if ! IFS= read -r actual_comm < "$process/comm" 2>/dev/null; then
      [ ! -d "$process" ] && continue; return 1
    fi
    [ "$actual_comm" = "$comm" ] || return 1
    if ! read_process_uid "$process"; then [ ! -d "$process" ] && continue; return 1; fi
    [ "$PROCESS_UID" = "$CAPTUREUID" ] || return 1
    scan_budget || return 1
    if [ "$comm" = atrace ]; then LEGACYSTATUS=legacy_atrace_active; return 1; fi
    case "$comm" in rm|rmdir|mkdir|mkfifo|unlink|mv|busybox|toybox)
      case "
$arguments
" in *"
$LOCK
"*|*"
$LOCK/"*) LEGACYSTATUS=legacy_workspace_active; return 1;; esac;;
    esac
    case "$comm" in timeout|busybox|toybox)
      case "
$arguments
" in *"
--async_stop
"*) case "
$arguments
" in *"
atrace
"*|*"
/system/bin/atrace
"*) LEGACYSTATUS=legacy_atrace_active; return 1;; esac;; esac;;
    esac
    case "
$arguments
" in *"
$0
"*|*"
$LEGACY_SCRIPT
"*) LEGACYSTATUS=legacy_helper_active; return 1;; esac
  done
  # The final candidate/read must not silently spend more than the fixed budget.
  scan_budget
}

read_owner_record() {
  owner_meta=$(stat -c '%d:%i:%u:%h:%s:%Y' "$LOCK/owner" 2>/dev/null) || return 1
  # Derive length from the exact snapshot validated against the pinned FD, not
  # a separate earlier stat that could precede an appended NUL byte.
  owner_size_fields=${owner_meta%:*}; owner_size=${owner_size_fields##*:}
  case "$owner_size" in ''|*[!0-9]*) return 1;; esac
  [ "$owner_size" -le 201 ] || return 1
  # Pin and validate before reading. Both the open and the bounded byte read are
  # timed; a FIFO replacement cannot hang recovery or grow an awk record in RAM.
  owner_framed=$(timeout -s KILL 1 sh -c '
    exec 9< "$1" || exit 1
    [ ! -L "$1" ] && [ -f /proc/self/fd/9 ] &&
      [ "$(stat -L -c "%d:%i:%u:%h:%s:%Y" /proc/self/fd/9 9<&9)" = "$2" ] || exit 1
    head -c 201 /proc/self/fd/9 9<&9 || exit 1
    [ ! -L "$1" ] && [ "$(stat -c "%d:%i:%u:%h:%s:%Y" "$1")" = "$2" ] &&
      [ "$(stat -L -c "%d:%i:%u:%h:%s:%Y" /proc/self/fd/9 9<&9)" = "$2" ] || exit 1
    printf "\ncodec_owner_read_end"
  ' codec_owner "$LOCK/owner" "$owner_meta" 8<&8 2>/dev/null) || return 1
  case "$owner_framed" in *"
codec_owner_read_end") OWNER_RECORD=${owner_framed%"
codec_owner_read_end"};; *) return 1;; esac
  owner_newline=0
  case "$OWNER_RECORD" in *"
") OWNER_RECORD=${OWNER_RECORD%"
"}; owner_newline=1;; esac
  case "$OWNER_RECORD" in *"
"*) return 1;; esac
  # Shell substitutions discard NUL bytes. Never let a torn binary record turn
  # into a valid token and bypass the malformed-owner initialization grace.
  if [ "$owner_size" -ne "$((${#OWNER_RECORD}+owner_newline))" ]; then
    OWNER_RECORD=malformed_binary_owner
  fi
  [ "${#OWNER_RECORD}" -le 200 ]
}
valid_owner() {
  oldpid=${previous_owner%%:*}; rest=${previous_owner#*:}
  oldstart=${rest%%:*}; rest=${rest#*:}
  oldsession=${rest%%:*}; oldsequence=${rest#*:}
  [ "$previous_owner" = "$oldpid:$oldstart:$oldsession:$oldsequence" ] || return 1
  case "$oldpid" in ''|0|*[!0-9]*) return 1;; esac
  case "$oldstart" in ''|*[!0-9]*) return 1;; esac
  case "$oldsession" in ''|*[!A-Za-z0-9_.-]*) return 1;; esac
  case "$oldsequence" in ''|*[!0-9]*) return 1;; esac
}
acquire_guard() {
  # The inode is permanent. Unlinking a flock file would let a second invocation
  # lock a replacement inode while the first capture still owns the original.
  if [ ! -e "$GUARD" ] && [ ! -L "$GUARD" ]; then
    (set -C; : > "$GUARD") 2>/dev/null || :
  fi
  [ -f "$GUARD" ] && [ ! -L "$GUARD" ] && [ "$(stat -c '%h' "$GUARD" 2>/dev/null)" = 1 ] || {
    LOCKSTATUS=unsafe_guard; STATUS=lock_unavailable; return 1;
  }
  GUARDID=$(stat -c '%d:%i:%u:%a' "$GUARD" 2>/dev/null)
  case "$GUARDID" in ''|*[!0-9:]*) LOCKSTATUS=unsafe_guard; STATUS=lock_unavailable; return 1;; esac
  case "$GUARDID" in *":$CAPTUREUID:600") ;; *)
    LOCKSTATUS=unsafe_guard; STATUS=lock_unavailable; return 1;; esac
  # Reading the stable regular file does not truncate or rewrite another owner.
  { exec 8< "$GUARD"; } 2>/dev/null || {
    LOCKSTATUS=guard_unavailable; STATUS=lock_unavailable; return 1;
  }
  [ "$(stat -L -c '%d:%i:%u:%a' /proc/$$/fd/8 2>/dev/null)" = "$GUARDID" ] || {
    LOCKSTATUS=unsafe_guard; STATUS=lock_unavailable; return 1;
  }
  # mksh keeps exec-opened descriptors close-on-exec unless the command receives
  # an explicit redirection. Pass the existing open description, not a new inode.
  flock -n 8 8<&8 2>/dev/null || { LOCKSTATUS=guard_busy; STATUS=capture_busy; return 1; }
  [ ! -L "$GUARD" ] && [ "$(stat -c '%d:%i:%u:%a' "$GUARD" 2>/dev/null)" = "$GUARDID" ] || {
    LOCKSTATUS=unsafe_guard; STATUS=lock_unavailable; return 1;
  }
  GUARDED=1
}
legacy_layout() {
  # This is the exact pre-guard layout, not permission normalization for arbitrary
  # trees. A previous interrupted tightening may have left only the owner at 666.
  for legacy_member in "$LOCK"/* "$LOCK"/.[!.]* "$LOCK"/..?*; do
    [ -e "$legacy_member" ] || [ -L "$legacy_member" ] || continue
    [ ! -L "$legacy_member" ] || return 1
    case "$legacy_member" in
      "$LOCK/owner") [ -f "$legacy_member" ] || return 1
        case "$(stat -c '%u:%h:%a' "$legacy_member" 2>/dev/null)" in "$CAPTUREUID":1:666|"$CAPTUREUID":1:600) ;; *) return 1;; esac;;
      "$LOCK/summary") [ -f "$legacy_member" ] &&
        [ "$(stat -c '%u:%h:%a' "$legacy_member" 2>/dev/null)" = "$CAPTUREUID:1:666" ] || return 1;;
      "$LOCK/pipe") [ -p "$legacy_member" ] &&
        [ "$(stat -c '%u:%h:%a' "$legacy_member" 2>/dev/null)" = "$CAPTUREUID:1:666" ] || return 1;;
      *) return 1;;
    esac
  done
  [ -f "$LOCK/owner" ] && [ -f "$LOCK/summary" ] && [ -p "$LOCK/pipe" ] || return 1
  legacy_owner_size=$(stat -c '%s' "$LOCK/owner" 2>/dev/null)
  legacy_summary_size=$(stat -c '%s' "$LOCK/summary" 2>/dev/null)
  case "$legacy_owner_size:$legacy_summary_size" in ''|*[!0-9:]*) return 1;; esac
  [ -n "$legacy_owner_size" ] && [ -n "$legacy_summary_size" ] &&
    [ "$legacy_owner_size" -le 200 ] && [ "$legacy_summary_size" -le 8192 ]
}
legacy_manifest() {
  stat -c '%d:%i:%u:%h:%s:%Y' "$LOCK/owner" "$LOCK/summary" "$LOCK/pipe" 2>/dev/null
}
legacy_member_worker() {
  timeout -s KILL 4 sh -c '
  lock=$1; guard=$2; guard_id=$3; directory=$4
  owner=$5; summary=$6; pipe=$7; trace=$8; operation=$9
  shift 9; directory_mode=$1; expected=$2
  exec 9< "$lock/owner" || exit 1
  exec 6< "$lock/summary" || exit 1
  matches() {
    [ ! -L "$lock" ] && [ ! -L "$lock/owner" ] && [ ! -L "$lock/summary" ] && [ ! -L "$lock/pipe" ] &&
    [ -f /proc/self/fd/9 ] && [ -f /proc/self/fd/6 ] && [ -p "$lock/pipe" ] &&
    [ "$(stat -c "%d:%i:%u:%a" "$lock" 2>/dev/null)" = "$directory:$directory_mode" ] &&
    [ "$(stat -L -c "%d:%i:%u" /proc/self/fd/7 7<&7 2>/dev/null)" = "$directory" ] &&
    [ "$(stat -L -c "%d:%i:%u:%h:%s:%Y" /proc/self/fd/9 9<&9 2>/dev/null)" = "$owner" ] &&
    [ "$(stat -L -c "%d:%i:%u:%h:%s:%Y" /proc/self/fd/6 6<&6 2>/dev/null)" = "$summary" ] &&
    [ "$(stat -c "%d:%i:%u:%h:%s:%Y" "$lock/owner" 2>/dev/null)" = "$owner" ] &&
    [ "$(stat -c "%d:%i:%u:%h:%s:%Y" "$lock/summary" 2>/dev/null)" = "$summary" ] &&
    [ "$(stat -c "%d:%i:%u:%h:%s:%Y" "$lock/pipe" 2>/dev/null)" = "$pipe" ] &&
    [ ! -L "$guard" ] && [ "$(stat -c "%d:%i:%u:%a" "$guard" 2>/dev/null)" = "$guard_id" ]
  }
  trace_off() {
    read state < "$trace/tracing_on" 2>/dev/null && read tracer < "$trace/current_tracer" 2>/dev/null &&
      [ "$state" = 0 ] && [ "$tracer" = nop ]
  }
  hashes() { timeout -s KILL 1 sha256sum /proc/self/fd/9 /proc/self/fd/6 9<&9 6<&6 8<&8 2>/dev/null; }
  matches || exit 1
  before=$(hashes) || exit 1
  matches && trace_off || exit 1
  if [ "$operation" = hash ]; then printf "%s\n" "$before"; exit 0; fi
  [ "$operation" = tighten ] && [ "$before" = "$expected" ] || exit 1
  chmod 600 /proc/self/fd/9 9<&9 8<&8 || exit 1
  after=$(hashes) || exit 1
  matches && [ "$after" = "$before" ] && trace_off || exit 1
  ' legacy "$LOCK" "$GUARD" "$GUARDID" "$legacy_directory" "$legacy_owner" "$legacy_summary" "$legacy_pipe" "$TRACEPATH" "$1" "$2" "$3" 7<&7 8<&8
}
tighten_legacy_permissions() {
  for legacy_tool in chmod sha256sum; do command -v "$legacy_tool" >/dev/null 2>&1 || return 1; done
  legacy_mode=$(stat -c '%a' "$LOCK" 2>/dev/null)
  case "$legacy_mode:$(stat -c '%a' "$LOCK/owner" 2>/dev/null)" in
    777:666|700:666) ;; *) return 1;;
  esac
  legacy_layout || return 1
  changed=$(stat -c '%Y' "$LOCK" 2>/dev/null); now=$(date +%s 2>/dev/null)
  case "$changed:$now" in ''|*[!0-9:]*) return 1;; esac
  [ -n "$changed" ] && [ -n "$now" ] && [ "$now" -ge "$changed" ] && [ "$((now-changed))" -ge 30 ] || return 1
  legacy_directory=$(stat -c '%d:%i:%u' "$LOCK" 2>/dev/null) || return 1
  [ "$legacy_directory" = "$previous_id:$CAPTUREUID" ] || return 1
  legacy_members=$(legacy_manifest) || return 1
  legacy_owner=$(stat -c '%d:%i:%u:%h:%s:%Y' "$LOCK/owner" 2>/dev/null) || return 1
  legacy_summary=$(stat -c '%d:%i:%u:%h:%s:%Y' "$LOCK/summary" 2>/dev/null) || return 1
  legacy_pipe=$(stat -c '%d:%i:%u:%h:%s:%Y' "$LOCK/pipe" 2>/dev/null) || return 1
  legacy_ok=0
  # The trailing /. requires a directory: a FIFO substitution cannot block this
  # readonly pin. Its identity must still match before any chmod is attempted.
  if { exec 7< "$LOCK/."; } 2>/dev/null; then
    if [ "$(stat -L -c '%d:%i:%u' /proc/self/fd/7 7<&7 2>/dev/null)" = "$legacy_directory" ] &&
        [ ! -L "$LOCK" ] && [ "$(stat -c '%d:%i:%u' "$LOCK" 2>/dev/null)" = "$legacy_directory" ] &&
        legacy_layout && [ "$(legacy_manifest)" = "$legacy_members" ]; then
      read_state
      if [ "$STATE" = 0 ] && [ "$TRACER" = nop ] &&
          legacy_content=$(legacy_member_worker hash "$legacy_mode" "") && chmod 700 /proc/self/fd/7 7<&7 8<&8 &&
          legacy_layout && [ "$(legacy_manifest)" = "$legacy_members" ] &&
          [ "$(stat -c '%d:%i:%u:%a' "$LOCK" 2>/dev/null)" = "$legacy_directory:700" ] &&
          legacy_helpers_absent; then
        read_state
        if [ "$STATE" = 0 ] && [ "$TRACER" = nop ]; then
          # Only this bounded child opens possibly substituted members. It has
          # no process-discovery exemption and never writes their contents. Both
          # pins are checked before the first hash; chmod targets the pinned FD.
          if legacy_member_worker tighten 700 "$legacy_content"; then
            if legacy_layout && [ "$(legacy_manifest)" = "$legacy_members" ] &&
                [ "$(stat -c '%d:%i:%u:%a' "$LOCK" 2>/dev/null)" = "$legacy_directory:700" ] &&
                [ "$(stat -c '%a' "$LOCK/owner" 2>/dev/null)" = 600 ]; then
              read_state
              [ "$STATE" != 0 ] || [ "$TRACER" != nop ] || legacy_ok=1
            fi
          fi
        fi
      fi
    fi
  fi
  exec 7<&-
  [ "$legacy_ok" = 1 ]
}

acquire_workspace() {
  LOCKSTATUS=acquired
  if ! mkdir "$LOCK" 8<&8 2>/dev/null; then
    [ -d "$LOCK" ] && [ ! -L "$LOCK" ] || {
      LOCKSTATUS=unsafe_workspace; STATUS=lock_unavailable; return 1;
    }
    legacy_permissions=0
    attributes=$(stat -c '%u:%a' "$LOCK" 2>/dev/null)
    case "$attributes" in "$CAPTUREUID":777) legacy_permissions=1;; "$CAPTUREUID":7[0145][0145]) ;; *)
      LOCKSTATUS=unsafe_workspace; STATUS=lock_unavailable; return 1;; esac
    [ -r "$LOCK" ] && [ -w "$LOCK" ] && [ -x "$LOCK" ] && safe_members || {
      LOCKSTATUS=unsafe_workspace; STATUS=lock_unavailable; return 1;
    }
    previous_id=$(stat -c '%d:%i' "$LOCK" 2>/dev/null)
    previous_owner=
    if [ -e "$LOCK/owner" ]; then
      permissions=$(stat -c '%u:%a' "$LOCK/owner" 2>/dev/null)
      case "$permissions" in "$CAPTUREUID":666) legacy_permissions=1;; "$CAPTUREUID":[46][04][04]) ;; *)
        LOCKSTATUS=owner_unreadable; STATUS=lock_owner_unavailable; return 1;; esac
      [ -r "$LOCK/owner" ] || {
        LOCKSTATUS=owner_unreadable; STATUS=lock_owner_unavailable; return 1;
      }
      read_owner_record || {
        LOCKSTATUS=owner_unreadable; STATUS=lock_owner_unavailable; return 1;
      }
      previous_owner=$OWNER_RECORD
      LOCKSTATUS=recovered_malformed_owner
    else
      LOCKSTATUS=recovered_missing_owner
    fi
    if [ "$legacy_permissions" = 1 ] && ! legacy_layout; then
      LOCKSTATUS=unsafe_workspace; STATUS=lock_unavailable; return 1
    fi
    if valid_owner; then
      if [ -d "/proc/$oldpid" ]; then
        actual_start=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$oldpid/stat" 2>/dev/null)
        case "$actual_start" in ''|*[!0-9]*)
          LOCKSTATUS=owner_unknown; STATUS=lock_owner_unavailable; return 1;; esac
        if [ "$actual_start" = "$oldstart" ]; then
          LOCKSTATUS=live_owner; STATUS=capture_busy; return 1
        fi
      elif [ ! -r /proc/1/stat ]; then
        LOCKSTATUS=owner_unknown; STATUS=lock_owner_unavailable; return 1
      fi
      LOCKSTATUS=recovered_dead_owner
    else
      # A partial token naming an extant process is still unknown ownership,
      # never evidence that its directory is safe to take.
      case "$oldpid" in ''|0|*[!0-9]*) ;;
        *) if [ -d "/proc/$oldpid" ]; then
          LOCKSTATUS=owner_unknown; STATUS=lock_owner_unavailable; return 1
        fi;;
      esac
      # Legacy initializers did not use the kernel guard. Do not take a young
      # empty/partial directory from an invocation still publishing its owner.
      changed=$(stat -c '%Y' "$LOCK" 2>/dev/null); now=$(date +%s 2>/dev/null)
      case "$changed:$now" in ''|*[!0-9:]*)
        LOCKSTATUS=owner_unknown; STATUS=lock_owner_unavailable; return 1;; esac
      [ -n "$changed" ] && [ -n "$now" ] && [ "$now" -ge "$changed" ] && [ "$((now-changed))" -ge 30 ] || {
        LOCKSTATUS=initializing; STATUS=capture_busy; return 1;
      }
    fi
    # A recorded dead parent does not prove its legacy producer/parser children
    # exited. They never held the new guard and may not have enabled tracing yet.
    legacy_helpers_absent || { LOCKSTATUS=$LEGACYSTATUS; STATUS=lock_owner_unavailable; return 1; }
    if [ "$legacy_permissions" = 1 ] && ! tighten_legacy_permissions; then
      LOCKSTATUS=recovery_failed; STATUS=lock_recovery_failed; return 1
    fi
    read_state
    [ "$STATE" = 0 ] && [ "$TRACER" = nop ] || {
      LOCKSTATUS=trace_changed; STATUS=trace_busy_or_unavailable; return 1;
    }
    [ ! -L "$LOCK" ] && [ "$(stat -c '%d:%i' "$LOCK" 2>/dev/null)" = "$previous_id" ] && safe_members || {
      LOCKSTATUS=recovery_failed; STATUS=lock_recovery_failed; return 1;
    }
    current_owner=
    if [ -e "$LOCK/owner" ]; then
      read_owner_record || {
        LOCKSTATUS=recovery_failed; STATUS=lock_recovery_failed; return 1;
      }
      current_owner=$OWNER_RECORD
    fi
    [ "$current_owner" = "$previous_owner" ] || {
      LOCKSTATUS=recovery_failed; STATUS=lock_recovery_failed; return 1;
    }
    # Serialized by the permanent flock, never an unguarded stale-owner rm.
    remove_members && rmdir "$LOCK" 8<&8 2>/dev/null && mkdir "$LOCK" 8<&8 2>/dev/null || {
      LOCKSTATUS=recovery_failed; STATUS=lock_recovery_failed; return 1;
    }
  fi
  LOCKID=$(stat -c '%d:%i' "$LOCK" 2>/dev/null)
  case "$LOCKID" in ''|*[!0-9:]*) LOCKSTATUS=initialization_failed; STATUS=lock_initialization_failed; return 1;; esac
  CREATED=1
  printf '%s\n' "$TOKEN" > "$LOCK/owner" || {
    LOCKSTATUS=initialization_failed; STATUS=lock_initialization_failed; return 1;
  }
  OWNED=1
  owns_lock || { LOCKSTATUS=initialization_failed; STATUS=lock_initialization_failed; return 1; }
}
reap_watchdog() {
  [ -n "$WATCHPID" ] || return 0
  kill "$WATCHPID" 2>/dev/null
  wait "$WATCHPID"; WATCHRC=$?
  # Only the committed deadline path uses this reserved exit status. An atrace
  # SIGKILL exit alone does not identify which actor stopped its process.
  [ "$WATCHRC" != 124 ] || WATCHDOGFIRED=1
  WATCHPID=
}
finish() {
  trap - EXIT TERM INT
  # Reap every owned child before releasing the lock. No delayed atrace process
  # or watchdog may later stop a subsequent capture after a sampler restart.
  reap_watchdog
  if [ -n "$ATRACEPID" ]; then
    kill "$ATRACEPID" 2>/dev/null
    kill -KILL "$ATRACEPID" 2>/dev/null
    wait "$ATRACEPID" 2>/dev/null
  fi
  # Its independent timeout also covers a child blocked opening an orphan FIFO.
  # Do not kill the timeout supervisor and leave its parser child unbounded.
  if [ -n "$AWKPID" ]; then wait "$AWKPID" 2>/dev/null; fi
  read_state
  if [ "$STARTED" = 1 ] && owns_lock && [ "$STATE" = 1 ] && [ "$TRACER" = nop ]; then
    # Only this live invocation may clean up its interrupted capture. Skipped
    # captures and stale-lock recovery never issue an atrace stop command.
    timeout 1 atrace --async_stop 8<&8 >/dev/null 2>&1
    read_state
  fi
  OFF=na
  [ "$STATE" = 0 ] && OFF=1
  [ "$STATE" = 1 ] && OFF=0
  if [ "$STARTED" = 1 ] && [ "$OFF" != 1 ]; then STATUS=restore_failed; fi
  ENDMS=$(clock_ms); ENDMS=${ENDMS:-$STARTMS}
  ELAPSED=$((ENDMS-STARTMS))
  [ "$ELAPSED" -ge 0 ] || ELAPSED=0
  if owns_lock && [ -r "$SUMMARYFILE" ]; then read SUMMARY < "$SUMMARYFILE"; fi
  # The parser's output is a fixed whitelist of numeric fields. Error captures
  # retain counts/health flags, but cannot expose partially healthy latency.
  if [ "$STATUS" != ok ]; then
    SUMMARY=$(printf '%s\n' "$SUMMARY" | sed 's/codec_latency_med_ms=[^ ]*/codec_latency_med_ms=na/;s/codec_latency_p95_ms=[^ ]*/codec_latency_p95_ms=na/;s/codec_latency_max_ms=[^ ]*/codec_latency_max_ms=na/')
  fi
  MESSAGE="sample=$SESSION-codec-$SEQUENCE session=$SESSION schema=6 | event=codec_capture codec_capture_status=$STATUS codec_lock_status=$LOCKSTATUS codec_capture_start_ms=$STARTMS codec_capture_end_ms=$ENDMS codec_capture_ms=$ELAPSED codec_trace_off=$OFF codec_atrace_rc=$ATRACERC codec_watchdog_fired=$WATCHDOGFIRED $SUMMARY"
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$MESSAGE" >> "$LOGFILE"
  log -p "$PRIO" -t CarPlayTiming "$MESSAGE" 2>/dev/null
  if owns_workspace && safe_members; then
    remove_members
    rmdir "$LOCK" 8<&8 2>/dev/null
  fi
  exec 8<&-
}
trap finish EXIT
trap 'STATUS=interrupted; exit 0' TERM INT

for path in /sys/kernel/tracing /sys/kernel/debug/tracing; do
  if [ -r "$path/tracing_on" ]; then TRACEPATH=$path; break; fi
done
read_state
if [ "$STATE" != 0 ] || [ "$TRACER" != nop ]; then STATUS=trace_busy_or_unavailable; exit 0; fi
for tool in atrace awk timeout mkfifo flock stat id cat tr head; do
  command -v "$tool" >/dev/null 2>&1 || { STATUS=tool_unavailable; exit 0; }
done
CAPTUREUID=$(id -u 2>/dev/null)
case "$CAPTUREUID:$SELFSTART" in ''|*[!0-9:]*) STATUS=lock_unavailable; LOCKSTATUS=identity_unavailable; exit 0;; esac
[ -n "$CAPTUREUID" ] && [ -n "$SELFSTART" ] || { STATUS=lock_unavailable; LOCKSTATUS=identity_unavailable; exit 0; }
set -- $(pidof com.zjinnova.zlink 2>/dev/null)
[ "$#" = 1 ] || { STATUS=zlink_missing_or_ambiguous; exit 0; }
ZPID=$1
case "$ZPID" in ''|*[!0-9]*) STATUS=zlink_missing_or_ambiguous; exit 0;; esac
ZSTART=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$ZPID/stat" 2>/dev/null)
case "$ZSTART" in ''|*[!0-9]*) STATUS=zlink_identity_unavailable; exit 0;; esac
acquire_guard && acquire_workspace || exit 0
mkfifo "$PIPE" 8<&8 || { STATUS=pipe_failed; exit 0; }
# Recheck after acquiring our guard; never start over an already active tracer.
read_state
if [ "$STATE" != 0 ] || [ "$TRACER" != nop ]; then STATUS=trace_busy_or_unavailable; exit 0; fi

CODEC_PROGRAM=$(cat <<'CODEC_AWK'
# BEGIN_CODEC_AWK
function quantile(p, rank, lo, hi) {
  if (!matches) return "na"
  rank=1+p*(matches-1);lo=int(rank);hi=(lo<matches ? lo+1 : lo)
  return sprintf("%.3f",lat[lo]+(lat[hi]-lat[lo])*(rank-lo))
}
function qclass(label) {
  if(index(label,"com.zjinnova.zlink")) return 1
  if(label ~ /^SurfaceTexture[-(]/) return 2
  return 0
}
function identity(instance) {
  if(!(instance in codecs)) {codecs[instance]=++instances;if(instances>8)over=1}
}
{
  bytes+=length($0)+1
  if(NR>200000 || bytes>16777216) {over=1;exit}
  sub(/\r$/,"")
  if($0 ~ /entries-in-buffer\/entries-written:/) {
    h=$0;sub(/^.*entries-in-buffer\/entries-written:[ ]*/,"",h)
    split(h,a,"/");buffer=a[1]+0;sub(/[^0-9].*$/,"",a[2]);written=a[2]+0;headers++
    if(written>buffer)overwrite=1
  }
  if(!match($0,/[0-9]+\.[0-9]+:[ ]+(tracing_mark_write|print):[ ]*/))next
  prefix=substr($0,1,RSTART-1);head=substr($0,RSTART,RLENGTH)
  payload=substr($0,RSTART+RLENGTH);sub(/:.*/,"",head);stamp=head*1000
  tid=""
  if(match(prefix,/-[0-9]+[ ]+(\([ ]*[0-9-]+[ ]*\)[ ]+)?\[[0-9]+\]/)) {
    tid=substr(prefix,RSTART+1,RLENGTH-1);sub(/[ ].*/,"",tid)
  }
  if(payload=="E" || payload ~ /^E\|[0-9]+$/) {
    if(tid!="" && depth[tid]>0){delete stack[tid,depth[tid]];depth[tid]--}
    next
  }
  split(payload,p,"[|]")
  if((p[1]!="B" && p[1]!="C") || p[2] !~ /^[0-9]+$/ || p[2]!=zpid)next
  label=p[3]
  if(p[1]=="C") {
    cls=qclass(label)
    if(cls && p[4] ~ /^[0-9]+$/) {
      if(cls==1){window_counter_n++;if(p[4]+0>window_qmax)window_qmax=p[4]+0}
      if(cls==2){texture_counter_n++;if(p[4]+0>texture_qmax)texture_qmax=p[4]+0}
    }
    next
  }
  if(tid!="") {
    parent=stack[tid,depth[tid]]
    if(label ~ /: [0-9]+$/ && parent=="acquireBuffer") {
      consumer=label;sub(/: [0-9]+$/,"",consumer);cls=qclass(consumer)
      if(cls==2) {
        texture_acquire_n++
        if(consumer in acquired) {gap=stamp-acquired[consumer];if(gap>texture_gap)texture_gap=gap}
        acquired[consumer]=stamp
      }
      if(cls==1)window_acquire_n++
    }
    depth[tid]++
    if(depth[tid]>64){over=1;exit}
    stack[tid,depth[tid]]=label=="acquireBuffer" ? "acquireBuffer" : "other"
  }
  if(label=="updateTexImage")texture_update_n++
  if(label ~ /(^|::)dequeueOutputBuffer($|[-(])/)dequeue_output_n++
  if(label ~ /(^|::)releaseOutputBuffer($|[-(])/)release_output_n++
  if(label ~ /(^|::)renderOutputBuffer($|[-(])/)render_output_n++
  if(label !~ /^CCodecBufferChannel::(queue|onWorkDone)\(c2\.unisoc\.(avc|hevc)\.decoder#[0-9]+@ts=-?[0-9]+\)$/)next
  if(++codec_markers>2048){over=1;exit}
  instance=label;sub(/^[^(]*\(/,"",instance);sub(/@ts=.*/,"",instance);identity(instance)
  pts=label;sub(/^.*@ts=/,"",pts);sub(/\)$/, "",pts);pts+=0
  key=instance SUBSEP sprintf("%.0f",pts)
  if(label ~ /::queue\(/) {
    input_n++;input_count[key]++
    if(input_count[key]==1)input_time[key]=stamp
    if(input_count[key]==2)duplicate_input_n++
    if(instance in input_last) {
      gap=stamp-input_last[instance];if(gap>input_gap)input_gap=gap
      if(pts<input_pts_last[instance])pts_resets++
    } else input_min[instance]=pts
    input_last[instance]=stamp;input_pts_last[instance]=pts
    if(pts<input_min[instance])input_min[instance]=pts
    if(!(instance in input_max) || pts>input_max[instance])input_max[instance]=pts
  } else {
    output_n++;output_count[key]++
    if(output_count[key]==1)output_time[key]=stamp
    if(output_count[key]==2)duplicate_output_n++
    if(instance in output_last) {
      gap=stamp-output_last[instance];if(gap>output_gap)output_gap=gap
      if(pts<output_pts_last[instance])output_reorder_n++
    }
    output_last[instance]=stamp;output_pts_last[instance]=pts
    if(!(instance in output_max) || pts>output_max[instance])output_max[instance]=pts
  }
}
END {
  for(key in input_count) {
    split(key,k,SUBSEP);instance=k[1];pts=k[2]+0
    if(!(key in output_count)) {
      unmatched_input_n++
      if(instance in output_max && pts>output_max[instance])input_tail_n++
    } else if(input_count[key]==1 && output_count[key]==1) {
      delta=output_time[key]-input_time[key]
      if(delta<0){negative_n++;continue}
      lat[++matches]=delta
    }
  }
  for(key in output_count)if(!(key in input_count)) {
    split(key,k,SUBSEP);instance=k[1];pts=k[2]+0;unmatched_output_n++
    if(instance in input_min && pts<input_min[instance])output_head_n++
  }
  for(i=2;i<=matches;i++){v=lat[i];j=i-1;while(j>=1 && lat[j]>v){lat[j+1]=lat[j];j--}lat[j+1]=v}
  status="ok"
  if(!codec_markers)status="no_markers"
  if(!headers)status="header_missing"
  if(overwrite)status="trace_overwrite"
  if(pts_resets)status="pts_reset"
  if(negative_n)status="negative_latency"
  if(over)status="limit_exceeded"
  med=quantile(.5);p95=quantile(.95);maximum=matches?sprintf("%.3f",lat[matches]):"na"
  if(status!="ok")med=p95=maximum="na"
  printf "codec_stats_status=%s codec_trace_entries=%s codec_trace_written=%s codec_trace_overwrite=%s codec_instances=%d codec_input_n=%d codec_output_n=%d codec_matched_n=%d codec_latency_med_ms=%s codec_latency_p95_ms=%s codec_latency_max_ms=%s",status,headers?buffer:"na",headers?written:"na",headers?overwrite+0:"na",instances,input_n,output_n,matches,med,p95,maximum
  printf " codec_input_gap_max_ms=%s codec_output_gap_max_ms=%s codec_duplicate_input_n=%d codec_duplicate_output_n=%d codec_pts_reset_n=%d codec_output_reorder_n=%d codec_negative_latency_n=%d",(input_n>1?sprintf("%.3f",input_gap):"na"),(output_n>1?sprintf("%.3f",output_gap):"na"),duplicate_input_n,duplicate_output_n,pts_resets,output_reorder_n,negative_n
  printf " codec_unmatched_input_n=%d codec_unmatched_output_n=%d codec_input_tail_n=%d codec_output_head_n=%d",unmatched_input_n,unmatched_output_n,input_tail_n,output_head_n
  printf " codec_texture_update_n=%d codec_texture_acquire_n=%d codec_texture_gap_max_ms=%s codec_window_acquire_n=%d codec_texture_counter_n=%d codec_texture_queue_max=%s codec_window_counter_n=%d codec_window_queue_max=%s",texture_update_n,texture_acquire_n,(texture_acquire_n>1?sprintf("%.3f",texture_gap):"na"),window_acquire_n,texture_counter_n,(texture_counter_n?texture_qmax+0:"na"),window_counter_n,(window_counter_n?window_qmax+0:"na")
  printf " codec_dequeue_output_n=%d codec_release_output_n=%d codec_render_output_n=%d\n",dequeue_output_n,release_output_n,render_output_n
}
# END_CODEC_AWK
CODEC_AWK
)
(
  # Close the guard before opening the FIFO, and put that potentially blocking
  # open inside the timeout's child. SIGKILL of the parent cannot wedge the lock
  # or leak a parser waiting forever for a producer that was never launched.
  exec 8<&-
  exec timeout -s KILL 10 sh -c 'exec awk -v zpid="$1" "$2" < "$3" > "$4"' codec "$ZPID" "$CODEC_PROGRAM" "$PIPE" "$SUMMARYFILE"
) 2>/dev/null &
AWKPID=$!
STARTMS=$(clock_ms); STARTMS=${STARTMS:-0}
read_state
if [ "$STATE" != 0 ] || [ "$TRACER" != nop ]; then STATUS=trace_busy_or_unavailable; exit 0; fi
STARTED=1
atrace -t 5 -b 2048 gfx video 8<&8 > "$PIPE" 2>/dev/null &
ATRACEPID=$!
ATRACESTART=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$ATRACEPID/stat" 2>/dev/null)
AWKSTART=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$AWKPID/stat" 2>/dev/null)
(
  sleep_pid=
  trap '[ -z "$sleep_pid" ] || { kill "$sleep_pid" 2>/dev/null; wait "$sleep_pid" 2>/dev/null; }; exit 0' TERM INT
  sleep 8 8<&8 &
  sleep_pid=$!
  wait "$sleep_pid" || exit 0
  sleep_pid=
  # Once the deadline commits, cancellation must not hide its outcome. The parent
  # reaps this bounded identity check before publishing the marker or releasing
  # the guard. Firing does not prove a producer was still alive or was killed.
  trap '' TERM INT
  # ATRACERC is collected before the parent clears this watchdog. The parent
  # reaps this watchdog before releasing the lock or starting another capture.
  for owned_child in "$ATRACEPID:$ATRACESTART"; do
    child_pid=${owned_child%%:*}; child_start=${owned_child#*:}
    [ -n "$child_pid" ] && [ -n "$child_start" ] || continue
    actual_start=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$child_pid/stat" 2>/dev/null)
    # A normally exited producer can have been reaped while awk drains its pipe.
    # Never signal a recycled PID at the overall eight-second deadline.
    [ "$actual_start" = "$child_start" ] && kill -KILL "$child_pid" 2>/dev/null
  done
  exit 124
) 8<&8 &
WATCHPID=$!
wait "$ATRACEPID"; ATRACERC=$?; ATRACEPID=
wait "$AWKPID"; AWKRC=$?; AWKPID=
reap_watchdog
STATUS=ok
[ "$ATRACERC" = 0 ] || STATUS=atrace_error
[ "$AWKRC" = 0 ] || STATUS=parser_error
ZEND=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$ZPID/stat" 2>/dev/null)
[ "$ZEND" = "$ZSTART" ] || STATUS=process_changed
if [ -r "$SUMMARYFILE" ]; then
  read SUMMARY < "$SUMMARYFILE"
  case "$SUMMARY" in
    codec_stats_status=ok\ *) ;;
    codec_stats_status=no_markers\ *) [ "$STATUS" != ok ] || STATUS=no_markers;;
    *) [ "$STATUS" != ok ] || STATUS=invalid_trace;;
  esac
else
  STATUS=summary_missing
fi
exit 0

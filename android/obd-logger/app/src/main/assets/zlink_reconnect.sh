#!/system/bin/sh
# Read the Q30's existing evidence once, after an observed ignition edge. Private
# identifiers stay in this process. Only the bonded classic address goes to the helper.
GUARD_DIR=/data/local/tmp/dashcam_wifi_startup
BACKUP_FLAG=/data/local/tmp/.dashcam_analyser_radios
package=com.zjinnova.zlink

skip() { echo "skip_$1"; exit 0; }

# Compare fixed-width civil timestamps from network_stack and media.metrics in the
# unit's own time zone. Logcat epoch stamps give the exact Bluetooth ordering.
local_time() { date -d "@$1" '+%Y-%m-%dT%H:%M:%S'; }
# Some Android services return partial text on timeout. The sentinel also
# distinguishes a command that exited nonzero after emitting parseable rows.
dump() {
    dumpsys -t "$1" "$2" ${3:+"$3"} 2>/dev/null && echo __complete__
}
clock_now() {
    now=$(date +%s) || skip clock
    case "$now" in ''|*[!0-9]*) skip clock ;; esac
    age=$((now - edge))
    [ "$age" -ge 20 ] && [ "$age" -le 55 ] || skip stale
}

check_gates() {
    [ -f "$GUARD_DIR/zlink_reconnect_enabled" ] || skip disabled
    for marker in "$BACKUP_FLAG" "$BACKUP_FLAG.watchdog_lease" "$BACKUP_FLAG.watchdog_active" "$BACKUP_FLAG.watchdog_ready"; do
        [ ! -e "$marker" ] || skip backup
    done
    [ "$(timeout 2 settings get global acc_status 2>/dev/null)" = 1 ] || skip ignition
    [ "$(timeout 2 settings get global bluetooth_on 2>/dev/null)" = 1 ] || skip bluetooth
    [ "$(timeout 2 settings get system persist.sys.carplay.connect.status 2>/dev/null)" = 0 ] || skip projection
    # Restrict recovery to the stock connection screen, excluding a different
    # ZLink activity (e.g. Help), the camera, Android settings, and other apps.
    dump 2 activity activities | awk '
        /^__complete__$/ { complete=1 }
        /DUMP TIMEOUT/ { timed_out=1 }
        /^[[:space:]]*topResumedActivity=/ {
            total++
            if ($0 ~ / u0 com\.zjinnova\.zlink\/com\.zjinnova\.android\.zlink\.features\.main\.MainActivity[[:space:]]/) main++
        }
        END { exit !(complete && !timed_out && total == 1 && main == 1) }
    ' || skip foreground
}

# Correlate the latest ZLink iAP service announcement to a socket closure in
# the same live process. Startup-page updateStates:CP messages before that close
# are routine; on a working connection another arrives immediately afterwards.
bluetooth_evidence() {
    { timeout 3 logcat -d -b main -v epoch "--pid=$pid" -s zj:E 2>/dev/null && echo __complete__; } |
        awk -v edge="$edge" -v now="$now" -v pid="$pid" '
        function valid_mac(mac, parts, i) {
            if (length(mac) != 17 || split(mac,parts,":") != 6) return 0
            for (i=1; i<=6; i++) if (parts[i] !~ /^[[:xdigit:]][[:xdigit:]]$/) return 0
            return toupper(mac) != "00:00:00:00:00:00" && toupper(mac) != "FF:FF:FF:FF:FF:FF"
        }
        /^__complete__$/ { complete=1 }
        $1 ~ /^[0-9]+\.[0-9][0-9][0-9]$/ && $1+0 >= edge && $1+0 < now+1 && $2 == pid && $4 == "E" && $5 == "zj" {
            if (index($0,"[zjBtService]device:")) {
                mac=$0
                sub(/^.*\[zjBtService\]device:[[:space:]]*/,"",mac)
                sub(/[[:space:]].*$/,"",mac)
                closed=""
                if (!valid_mac(mac) || index(tolower($0),"uuid:00000000-deca-fade-deca-deafdecacafe") == 0) mac=""
                if (mac && prior && toupper(mac) != toupper(prior)) ambiguous=1
                prior=mac
            }
            if (mac && index($0,"bt socket closed, read return: -1")) closed=$1
            if (closed && index($0,"updateStates:CP")) connected=1
        }
        END { if (complete && mac && closed && !connected && !ambiguous) print toupper(mac), closed }
    '
}

# The most recent on-device hotspot record for this saved wireless CarPlay
# peer must show a new lease after this Bluetooth close. Android also records
# other AP clients and prints the previous lease before "created/renewed lease".
has_late_lease() {
    dump 3 network_stack |
        awk -v mac="$wifi_mac" -v edge="$start.000" -v after="$closed_local" -v until="$grace_local" '
        /^__complete__$/ { complete=1 }
        /DUMP TIMEOUT/ { timed_out=1 }
        index($0,"[wlan2.DHCP.Repository] DHCPREQUEST assignedLease ") &&
        index($0,"created/renewed lease ") {
            stamp=substr($1,1,23)
            if (length(stamp) != 23 || stamp < edge || stamp > until) next
            sub(/^.*created\/renewed lease /,"")
            if (index(tolower($0),"hwaddr: " tolower(mac) ",") > 0) {
                if (stamp <= after) already_joined=1
                else found=1
            }
        }
        END { exit !(complete && !timed_out && found && !already_joined) }
    '
}

# Completed decoder lifetimes appear only on release. A separate current-client
# check below protects a session that is still alive, including one that starts
# between collecting the history and reaching the helper.
no_completed_session() {
    dump 3 media.metrics |
        awk -v start="$media_start" -v stop="$media_stop" '
        /^__complete__$/ { finished=1 }
        /DUMP TIMEOUT/ { timed_out=1 }
        /^Metrics gathering: enabled/ { enabled=1 }
        /^TransactionLog:/ { complete=1 }
        /\{codec, \(/ && /\(com\.zjinnova\.zlink,/ && /android.media.mediacodec.encoder=0,/ && /android.media.mediacodec.mime=video\// {
            ts=$0; sub(/^.*\{codec, \(/,"",ts); sub(/\).*/,"",ts)
            if (length(ts) != 18 || (ts >= start && ts <= stop)) seen=1
        }
        END { exit !(finished && !timed_out && enabled && complete && !seen) }
    '
}

no_active_session() {
    dump 2 media.resource_manager | awk -v pid="$pid" '
        /^__complete__$/ { finished=1 }
        /DUMP TIMEOUT/ { timed_out=1 }
        /^[[:space:]]*Processes:/ { processes=1 }
        /^[[:space:]]*Events logs/ { complete=1; processes=0 }
        processes && $1=="Pid:" { selected=($2==pid); if (selected) identified=1 }
        processes && selected && (/Name: .*\.decoder/ || /non-secure-codec\/video-codec/ || /secure-codec\/video-codec/) { active=1 }
        END { exit !(finished && !timed_out && complete && identified && !active) }
    '
}

[ "$#" = 1 ] || skip arguments
edge=$1
case "$edge" in ''|0*|*[!0-9]*) skip arguments ;; esac
[ "${#edge}" -le 11 ] || skip arguments
clock_now
check_gates
pid=$(pidof "$package") || skip process
case "$pid" in ''|0*|*[!0-9]*) skip process ;; esac
wifi_mac=$(getprop persist.zlink.carplay.mac)
printf '%s\n' "$wifi_mac" | awk -F: '
    NF == 6 && length($0)==17 && $0!="00:00:00:00:00:00" && toupper($0)!="FF:FF:FF:FF:FF:FF" {
        valid=1; for (i=1;i<=6;i++) if ($i !~ /^[[:xdigit:]][[:xdigit:]]$/) valid=0
    }
    END { exit !valid }
' || skip saved_peer
evidence=$(bluetooth_evidence)
[ -n "$evidence" ] || skip handshake
classic=${evidence% *}
closed=${evidence#* }
closed_local=$(local_time "${closed%.*}") || skip clock
closed_local="$closed_local.${closed#*.}"
grace_local=$(local_time "$((now - 20))") || skip clock
grace_local="$grace_local.000"
start=$(local_time "$edge") && stop=$(local_time "$now") || skip clock
has_late_lease || skip lease
# Media metrics omit the year. At a local New Year or DST-clock reversal the
# ordering cannot be recovered from those rows; wait for another ignition.
[ "${start%%-*}" = "${stop%%-*}" ] && [ "$start" != "$stop" ] || skip clock
media_start=$(printf '%s' "${start#*-}" | tr T ' ').000
media_stop=$(printf '%s' "${stop#*-}" | tr T ' ').999
[ "$media_start" \< "$media_stop" ] || skip clock
no_completed_session || skip decoder_history
check_gates
[ "$(pidof "$package")" = "$pid" ] || skip process
no_active_session || skip decoder
clock_now
# The helper owns the disconnect-to-reconnect window and its bounded waits.
# Killing it from outside after the first operation could strand the phone.
reply=$(app_process /system/bin com.dashcamstats.obdlogger.CarPlayPhoneBluetooth retry "$classic" 2>/dev/null) || skip phone
[ "$reply" = reconnect_requested ] || skip phone
echo reconnect_requested

#!/system/bin/sh
# The private binding is enrolled once from a confirmed projection. Lookup is used
# internally by the watcher; its output must never be written to the guard log.
root=/data/local/tmp/dashcam_wifi_startup
binding="$root/carplay_peer"
valid() {
    printf '%s\n' "$1" | awk -F: '
        NF==6 && length($0)==17 && $0!="00:00:00:00:00:00" && $0!="FF:FF:FF:FF:FF:FF" {
            for(i=1;i<=6;i++) if($i !~ /^[0-9A-F][0-9A-F]$/) exit 1
            ok=1
        }
        END { if (!ok || NR!=1) exit 1 }
    '
}
[ -d "$root" ] && [ ! -L "$root" ] && [ -f "$root/zlink_reconnect_enabled" ] || exit 1
wifi=$(printf '%s' "$2" | tr 'a-f' 'A-F')
valid "$wifi" || exit 1
[ "$(getprop persist.zlink.carplay.mac | tr 'a-f' 'A-F')" = "$wifi" ] || exit 1
case "$1" in
    lookup)
        [ "$#" = 2 ] && [ -f "$binding" ] && [ ! -L "$binding" ] || exit 1
        IFS=' ' read -r saved classic extra < "$binding" || exit 1
        [ "$wifi" = "$saved" ] && [ -z "$extra" ] && valid "$classic" || exit 1
        printf '%s\n' "$classic"
        ;;
    enroll)
        # Invoked by the parked provisioning tool after it correlates DHCP, the
        # successful iAP handshake and ZLink's video decoder. No radio operation.
        [ "$#" = 4 ] && valid "$3" && [ -n "$4" ] || exit 1
        [ "$(pidof com.zjinnova.zlink)" = "$4" ] || exit 1
        [ "$(settings get global acc_status)" = 0 ] || exit 1
        umask 077
        tmp=$(mktemp "$root/.carplay_peer.XXXXXX") || exit 1
        trap 'rm -f "$tmp"' EXIT
        printf '%s %s\n' "$wifi" "$3" > "$tmp" && mv -f "$tmp" "$binding" || exit 1
        echo enrolled
        ;;
    *) exit 1 ;;
esac

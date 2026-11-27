# One read of Android's existing histories on a parked visit. No phone or logger controls.
[ "$(settings get global acc_status 2>/dev/null)" = 0 ] || exit 0
printf 'clock %s %s\n' "$(date +%s)" "$(getprop persist.sys.timezone)"
wifi=$(getprop persist.zlink.carplay.mac | tr 'a-f' 'A-F')
valid_peer() {
    printf '%s\n' "$1" | awk -F: '
    NF==6 && length($0)==17 && $0!="00:00:00:00:00:00" && $0!="FF:FF:FF:FF:FF:FF" {
        for(i=1;i<=6;i++) if($i !~ /^[0-9A-F][0-9A-F]$/) exit 1
        ok=1
    } END {if(!ok || NR!=1)exit 1}'
}
root=/data/local/tmp/dashcam_wifi_startup
classic=
if [ ! -L "$root" ] && [ -f "$root/carplay_peer" ] && [ ! -L "$root/carplay_peer" ] &&
    IFS=' ' read -r enrolled enrolled_classic extra < "$root/carplay_peer" &&
    [ -z "$extra" ] && [ "$wifi" = "$enrolled" ] && valid_peer "$wifi" && valid_peer "$enrolled_classic"; then
    classic=$enrolled_classic
    { dumpsys -t 3 network_stack 2>/dev/null && echo __read_ok__; } |
    awk -v mac="$wifi" 'index($0,"[wlan2.DHCP.Repository] DHCPREQUEST assignedLease ") &&
        index($0,"created/renewed lease ") {
            stamp=substr($1,1,23)
            sub(/^.*created\/renewed lease /,"")
            if(index(toupper($0),"HWADDR: " mac ",") && stamp ~ /^[0-9:T.-]+$/)
                print "lease", stamp
        }
        /DUMP TIMEOUT/ {partial=1}
        /^__read_ok__$/ && !partial {print "network_complete"}' | tail -n 257
else
    echo network_complete
fi
{ dumpsys -t 3 media.metrics 2>/dev/null && echo __read_ok__; } |
    awk '/^[[:space:]]*[0-9]+: \{codec, \([^)]*\), \(com\.zjinnova\.zlink, / {
        n=split($0,f,", "); encoder=""; mime=""; lifetime=""; samples=0
        for(i=4;i<=n;i++) {
            field=f[i]; sub(/^\(/,"",field); sub(/[)}]+$/,"",field)
            split(field,a,"=")
            if(a[1]=="android.media.mediacodec.encoder") encoder=a[2]
            if(a[1]=="android.media.mediacodec.mime") mime=a[2]
            if(a[1]=="android.media.mediacodec.lifetimeMs") lifetime=a[2]
            if(a[1]=="android.media.mediacodec.latency.n" && a[2]~/^[0-9]+$/) samples=a[2]+0
        }
        stamp=f[2]; gsub(/[()]/,"",stamp); gsub(/ /,"T",stamp)
        if(encoder=="0" && mime~/^video\// && lifetime~/^[0-9]+$/ && stamp~/^[0-9T:.-]+$/)
            print (samples>0 ? "video_output" : "decoder"), stamp, lifetime
    }
    /DUMP TIMEOUT/ {partial=1}
    /^Metrics gathering: enabled/ {enabled=1}
    /^TransactionLog:/ {footer=1}
    /^__read_ok__$/ && !partial && enabled && footer {print "metrics_complete"}' | tail -n 257
pid=$(pidof com.zjinnova.zlink 2>/dev/null)
case "$pid" in ''|*[!0-9]*) pid=0;; esac
{ timeout 3 logcat -d -b main -v epoch -s 'zj:E' 'WifiStartupGuard:E' 2>/dev/null && echo __read_ok__; } |
    awk -v pid="$pid" -v classic="$classic" '$1~/^[0-9]+\.[0-9]+$/ && $4=="E" && $6==":" {
        if($5=="zj" && classic!="" && $2==pid) {
            if(index($7,"[zjBtService]device:")==1) {
                saved=(toupper($7)=="[ZJBTSERVICE]DEVICE:" classic &&
                    $8=="uuid:00000000-deca-fade-deca-deafdecacafe")
                closed=0; announced=$1+0
                if(saved) print "iap", $1
                else if ($8=="uuid:00000000-deca-fade-deca-deafdecacafe") print "other_iap", $1
            } else if(saved && $1+0>=announced && $1+0-announced<=15) {
                if(index($0,"bt socket closed, read return: -1")) closed=1
                else if(closed && index($0,"updateStates:CP")) {
                    print "phone_handoff", $1; saved=0
                }
            }
        }
        if($5=="WifiStartupGuard") {
            if($7=="hold_sample" && $8=="ap_up=true") print "hotspot", $1
            if(NF!=7) next
            if($7=="zlink_phone_reconnect_requested") print "early_retry", $1
            if($7=="zlink_phone_waiting_connect_requested") print "waiting_connect", $1
            if($7=="zlink_phone_waiting_cycle_requested") print "waiting_cycle", $1
        }
    }
    /^__read_ok__$/ {print "log_complete"}' | tail -n 257
echo done

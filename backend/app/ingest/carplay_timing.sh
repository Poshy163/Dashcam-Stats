#!/system/bin/sh
# dashcam-stats CarPlay timing sampler. Deleting this file, its .pid and its .log removes
# its diagnostics. It never changes radio, power, recorder or CarPlay settings.
# Short guarded ATrace windows observe codec progress; raw trace data is not retained.
#
# Every INTERVAL seconds, while a WLAN2 neighbour is present, it reads the frame timing of
# package-named Zlink application buffer layers. Window presentation timing still cannot
# prove CarPlay decode frame rate. Alongside it it reads
# the things that could be starving that path: load, SoC temperature, Zlink's own CPU, the
# hotspot's incoming bitrate, and which channels the two radio roles are on. One line per
# surface goes to the log file and, at priority PRIO, into logcat under the tag
# CarPlayTiming, so the unit-log collector ships it home on the next visit.
INTERVAL="${1:-15}"
PRIO="${2:-w}"
# SurfaceFlinger retains only 127 frames. Use a separate three-second deadline loop
# with headroom for the observed ~30 fps stream; slow diagnostics cannot delay it.
# Ring overlap and actual polling gaps are logged, so overload or faster surfaces
# cannot silently masquerade as continuous coverage.
FRAME_INTERVAL="${3:-3}"
MARKPFX=/data/local/tmp/.dashcam_cpt_seen_
LOG=/data/local/tmp/dashcam_carplay_timing.log
PIDF=/data/local/tmp/.dashcam_carplay_timing.pid
TAG=CarPlayTiming
# Bounded direct-file history is independent from logcat's small, noisy buffers.  It is
# read non-destructively and deduplicated when the unit returns home.
LOG_KIB=1024
LOG_ROTATIONS=8
SESSION="$(date +%s)-$(cut -d' ' -f1 /proc/uptime 2>/dev/null | tr -d .)-$$"
SCRIPT=/data/local/tmp/dashcam_carplay_timing.sh
CODEC_SCRIPT=/data/local/tmp/dashcam_carplay_codec.sh
BUILD_ID=__DASHCAM_SAMPLER_BUILD__
BUILDF=/data/local/tmp/.dashcam_carplay_timing.build
SEQF=/data/local/tmp/.dashcam_carplay_timing.seq
if [ -f "$PIDF" ]; then
  oldpid=$(cat "$PIDF" 2>/dev/null)
  case "$oldpid" in
    ''|*[!0-9]*) ;;
    *)
      if [ -r "/proc/$oldpid/cmdline" ] && tr '\000' ' ' < "/proc/$oldpid/cmdline" | grep -Fq "$SCRIPT"; then
        # Presence polls should not reset timing baselines or interrupt a trace window.
        if kill -0 "$oldpid" 2>/dev/null && [ "$(cat "$BUILDF" 2>/dev/null)" = "$BUILD_ID:$INTERVAL:$FRAME_INTERVAL" ]; then
          exit 0
        fi
        kill "$oldpid" 2>/dev/null
        attempts=0
        while kill -0 "$oldpid" 2>/dev/null && [ "$attempts" -lt 100 ]; do
          sleep 0.1; attempts=$((attempts+1))
        done
        # Never overlap the old sampler/trace cleanup with a replacement.
        kill -0 "$oldpid" 2>/dev/null && exit 1
      fi ;;
  esac
fi
echo $$ > "$PIDF"
echo "$BUILD_ID:$INTERVAL:$FRAME_INTERVAL" > "$BUILDF"
echo 0 > "$SEQF"

prev_ticks=0; prev_t=0; prev_zpid=; prev_rx=0; prev_rx_t=0; idle_n=0; prev_drops=-1
prev_oticks=0; prev_ot=0; prev_opid=; prev_neigh=na; prev_sta_mhz=na; prev_ap=na; started=0
prev_cticks=0; prev_ct=0; prev_cpid=
slow_pid=; slow_last=0; last_active=0
FRAME_CONTEXT=/data/local/tmp/.dashcam_cpt_context_$SESSION
FRAME_SEQ=/data/local/tmp/.dashcam_cpt_frame_seq_$SESSION
frame_pid=; link_pid=; codec_pid=; codec_start=; codec_last=0; codec_seq=0; sleep_pid=
frame_start=; link_start_ticks=; slow_start=; sleep_start=
process_start() { awk '{sub(/^.*\) /,"");print $20}' "/proc/$1/stat" 2>/dev/null; }
child_alive() {
  [ -n "$1" ] && [ -n "$2" ] && [ "$(process_start "$1")" = "$2" ] && kill -0 "$1" 2>/dev/null
}
codec_alive() {
  child_alive "$codec_pid" "$codec_start"
}
clock_ms() { awk '{printf "%.0f",$1*1000}' /proc/uptime; }
# Deadline scheduling subtracts work time. After an overrun, skip missed deadlines
# instead of issuing a burst of catch-up Binder calls.
deadline_delay() {
  awk -v deadline="$1" -v current="$2" -v step="$3" 'BEGIN {
    if(deadline<=current)deadline+=(int((current-deadline)/step)+1)*step
    printf "%.0f %.3f",deadline,(deadline-current)/1000
  }'
}
cleanup() {
  # Signal all owned children first, then reap them before a replacement can start.
  codec_alive && kill "$codec_pid" 2>/dev/null
  for child in "$frame_pid:$frame_start" "$link_pid:$link_start_ticks" "$slow_pid:$slow_start" "$sleep_pid:$sleep_start"; do
    child_alive "${child%:*}" "${child#*:}" && kill "${child%:*}" 2>/dev/null
  done
  if [ -n "$codec_pid" ]; then
    wait "$codec_pid" 2>/dev/null
  fi
  for child in "$frame_pid" "$link_pid" "$slow_pid" "$sleep_pid"; do
    [ -n "$child" ] && wait "$child" 2>/dev/null
  done
  rm -f "$FRAME_CONTEXT" "$FRAME_CONTEXT.new" "$FRAME_SEQ"
}
trap 'exit 0' TERM INT
trap cleanup EXIT
# All output is numeric aggregates. Never retain socket addresses, input events, screen
# contents or full process/codec dumps. Missing proc access is unavailable, not zero.
pressure() {
  awk '$1=="some" {for(i=2;i<=NF;i++) if($i ~ /^avg10=/) {split($i,a,"=");print a[2];exit}}' "/proc/pressure/$1" 2>/dev/null
}
diagnostic_context() {
  country=$(timeout 1 cmd wifi get-country-code 2>/dev/null | sed -n 's/^Wifi Country Code = \([A-Z][A-Z]\)$/\1/p')
  mem=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null)
  pcpu=$(pressure cpu); pio=$(pressure io); pmem=$(pressure memory)
  clocks=$(cat /sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq 2>/dev/null | awk '
    /^[0-9]+$/ {if(n==0 || $1<lo)lo=$1;if($1>hi)hi=$1;n++}
    END {if(n)printf "cpu_min_khz=%d cpu_max_khz=%d",lo,hi;else printf "cpu_min_khz=na cpu_max_khz=na"}')
  uid=na; rss=na; threads=na; queues="zlink_tcp_sockets=na zlink_rx_queue_bytes=na zlink_tx_queue_bytes=na"
  if [ -n "$zpid" ]; then
    rss=$(awk '/^VmRSS:/ {print $2}' /proc/$zpid/status 2>/dev/null)
    threads=$(awk '/^Threads:/ {print $2}' /proc/$zpid/status 2>/dev/null)
    uid=$(awk '/^Uid:/ {print $2}' /proc/$zpid/status 2>/dev/null)
    # Linux tcp tables expose the owning UID in column 8, queue bytes as hex in column
    # 5. Count the app UID only; other processes' sockets must not become ZLink evidence.
    if [ -n "$uid" ] && [ -r /proc/net/tcp ] && [ -r /proc/net/tcp6 ]; then
      queues=$(cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk -v uid="$uid" '
        function hex(s, v,i,c) {v=0;s=tolower(s);for(i=1;i<=length(s);i++){c=index("0123456789abcdef",substr(s,i,1))-1;if(c<0)return 0;v=v*16+c}return v}
        $2=="local_address" {headers++;next}
        $8==uid && $5 ~ /^[0-9A-Fa-f]+:[0-9A-Fa-f]+$/ {split($5,q,":");tx+=hex(q[1]);rx+=hex(q[2]);n++}
        END {if(headers==2)printf "zlink_tcp_sockets=%d zlink_rx_queue_bytes=%.0f zlink_tx_queue_bytes=%.0f",n,rx,tx;else printf "zlink_tcp_sockets=na zlink_rx_queue_bytes=na zlink_tx_queue_bytes=na"}')
    fi
  fi
  # Decoder service is shared by recording and playback: explicitly device-wide context.
  cpid=$(pidof media.unisoc.codec2 2>/dev/null | cut -d' ' -f1); ccpu=na
  if [ -n "$cpid" ] && [ -r /proc/$cpid/stat ]; then
    cticks=$(sed 's/.*) //' /proc/$cpid/stat 2>/dev/null | awk '{print $12+$13}')
    if [ "$prev_cpid" = "$cpid" ] && [ "$prev_ct" -gt 0 ] && [ -n "$cticks" ] && [ "$cticks" -ge "$prev_cticks" ]; then
      cdt=$((now-prev_ct)); [ "$cdt" -gt 0 ] && ccpu=$(( (cticks-prev_cticks)/cdt ))
    fi
    prev_cticks=${cticks:-0}; prev_ct=$now; prev_cpid=$cpid
  else
    prev_cticks=0; prev_ct=0; prev_cpid=
  fi
  transport=$(timeout 2 ss -tine 2>/dev/null | tcp_summary)
  display_queue=$(dumpsys -t 2 SurfaceFlinger 2>/dev/null | surface_queue_summary)
  sched="zlink_main_runtime_ns=na zlink_main_wait_ns=na zlink_start_ticks=na"
  if [ -n "$zpid" ]; then
    sched=$(awk 'NF==3 && $1~/^[0-9]+$/ && $2~/^[0-9]+$/ {printf "zlink_main_runtime_ns=%s zlink_main_wait_ns=%s",$1,$2;found=1} END {if(!found)printf "zlink_main_runtime_ns=na zlink_main_wait_ns=na"}' /proc/$zpid/schedstat 2>/dev/null)
    start_ticks=$(sed 's/.*) //' /proc/$zpid/stat 2>/dev/null | awk '{print $20}')
    sched="$sched zlink_start_ticks=${start_ticks:-na}"
  fi
  diag="schema=6 wifi_country_code=${country:-na} mem_available_kib=${mem:-na} cpu_pressure=${pcpu:-na} io_pressure=${pio:-na} memory_pressure=${pmem:-na} $clocks zlink_rss_kib=${rss:-na} zlink_threads=${threads:-na} $queues decoder_cpu=${ccpu:-na} $transport $display_queue $sched"
}
tcp_summary() {
  # ss exposes TCP_INFO per socket. Match the exact app UID on the socket header,
  # then parse only numeric fields on its indented detail line. Never emit endpoints.
  awk -v uid="${uid:-na}" '
    /^State[ \t]+Recv-Q/ {header=1;next}
    /^[^ \t]/ {
      selected=0
      if($1!="ESTAB")next
      for(i=1;i<=NF;i++)if($i=="uid:" uid)selected=1
      if(selected)n++
      next
    }
    selected && /^[ \t]/ {
      for(i=1;i<=NF;i++) {
        if($i~/^rtt:[0-9.]+\/[0-9.]+$/) {split(substr($i,5),r,"/");if(!nr || r[1]+0>rmax)rmax=r[1]+0;nr++}
        if($i~/^retrans:[0-9]+\/[0-9]+$/) {split(substr($i,9),r,"/");pending+=r[1];total+=r[2];nt++}
        if($i~/^rto:[0-9.]+$/) {v=substr($i,5)+0;if(!no || v>rto)rto=v;no++}
      }
      selected=0
    }
    END {
      ok=header && uid~/^[0-9]+$/
      printf "zlink_tcp_info_sockets=%s zlink_tcp_rtt_max_ms=%s zlink_tcp_rto_max_ms=%s zlink_tcp_retrans_pending=%s zlink_tcp_retrans_total=%s",
        (ok?n+0:"na"),(ok && nr?rmax:"na"),(ok && no?rto:"na"),(ok && nt?pending:"na"),(ok && nt?total:"na")
    }'
}
peer_tcp_summary() {
  # IPv4 only, exact UID AND a resolved wlan2 neighbour. Addresses exist only in
  # memory. Idle/control sockets are not evidence of current video round-trip time.
  awk -v uid="${link_uid:-na}" -v peers="$link_peers" -v ok="$link_ok" '
    BEGIN {split(peers,p," ");for(i in p)if(p[i]!="")allowed[p[i]]=1}
    /^State[ \t]+Recv-Q/ {header=1;next}
    /^[^ \t]/ {
      selected=0
      if($1!="ESTAB")next
      owner=0;for(i=1;i<=NF;i++)if($i=="uid:" uid)owner=1
      peer=$5;sub(/:[0-9]+$/,"",peer)
      if(!owner || !(peer in allowed) || $2!~/^[0-9]+$/ || $3!~/^[0-9]+$/)next
      selected=1;n++;rx+=$2;tx+=$3;next
    }
    selected && /^[ \t]/ {
      rtt=-1;age=-1
      for(i=1;i<=NF;i++) {
        if($i~/^rtt:[0-9.]+\/[0-9.]+$/) {split(substr($i,5),r,"/");rtt=r[1]+0;if(!nr || rtt>rmax)rmax=rtt;nr++}
        if($i~/^lastrcv:[0-9]+$/) {age=substr($i,9)+0;if(!na || age<amin)amin=age;na++}
        if($i~/^bytes_received:[0-9]+$/) {bytes+=substr($i,16)+0;nb++}
      }
      if(age>=0 && age<=1000 && rtt>=0) {if(!ar || rtt>amax)amax=rtt;ar++}
      selected=0
    }
    END {
      valid=ok==1 && header && uid~/^[0-9]+$/
      printf "peer_tcp_sockets=%s peer_rx_queue_bytes=%s peer_tx_queue_bytes=%s peer_tcp_rtt_max_ms=%s peer_recent_rtt_max_ms=%s peer_receive_age_min_ms=%s peer_bytes_received_total=%s",
        (valid?n+0:"na"),(valid?rx+0:"na"),(valid?tx+0:"na"),(valid && nr?rmax:"na"),
        (valid && ar?amax:"na"),(valid && na?amin:"na"),(valid && nb?sprintf("%.0f",bytes):"na")
    }'
}
transport_v6_summary() {
  # The suffix after | is private continuity state, retained only in link_loop RAM.
  # Root ownership requires the same proc inode AND normalized endpoint tuple.
  # A missing ss UID is unknown, never implicitly root. Byte deltas require the
  # exact same socket membership and complete consecutive observations.
  awk -v uid="${link_uid:-na}" -v previous="$transport_state" -v stamp="$link_start" '
    function hex(s, v,i,c) {v=0;s=tolower(s);for(i=1;i<=length(s);i++){c=index("0123456789abcdef",substr(s,i,1))-1;if(c<0)return -1;v=v*16+c}return v}
    function v4(s, a,n,i,out) {n=split(s,a,".");if(n!=4)return "";out="4";for(i=1;i<=4;i++){if(a[i]!~/^[0-9]+$/ || a[i]+0>255)return "";out=out sprintf("%02x",a[i])}return out}
    function ip(s, h,l,r,n,nl,nr,z,i,x,out,p,t) {
      gsub(/[\[\]]/,"",s);sub(/%.*/,"",s);s=tolower(s)
      if(index(s,":")==0)return v4(s)
      if(index(s,".")){p=0;for(i=1;i<=length(s);i++)if(substr(s,i,1)==":")p=i;t=v4(substr(s,p+1));if(t=="")return "";s=substr(s,1,p) substr(t,2,4) ":" substr(t,6,4)}
      n=split(s,h,"::");if(n>2)return "";nl=(h[1]==""?0:split(h[1],l,":"));nr=(n==2 && h[2]!=""?split(h[2],r,":"):0)
      z=8-nl-nr;if((n==1 && z!=0) || (n==2 && z<1))return ""
      out="";for(i=1;i<=nl;i++){x=l[i];if(x!~/^[0-9a-f]+$/ || length(x)>4)return "";out=out sprintf("%04x",hex(x))}
      for(i=1;i<=z;i++)out=out "0000"
      for(i=1;i<=nr;i++){x=r[i];if(x!~/^[0-9a-f]+$/ || length(x)>4)return "";out=out sprintf("%04x",hex(x))}
      return (substr(out,1,24)=="00000000000000000000ffff"?"4" substr(out,25):"6" out)
    }
    function endpoint(s, p,h,k) {p=s;sub(/^.*:/,"",p);if(p!~/^[0-9]+$/)return "";h=substr(s,1,length(s)-length(p)-1);k=ip(h);return(k!=""?k ":" p:"")}
    function zone(s, p) {sub(/:[^:]*$/,"",s);gsub(/[\[\]]/,"",s);p=index(s,"%");return(p?substr(s,p+1):"")}
    function proc_endpoint(s, a,h,out,i,j,k) {split(s,a,":");h=tolower(a[1]);if((length(h)!=8 && length(h)!=32) || h!~/^[0-9a-f]+$/ || a[2]!~/^[0-9a-fA-F]+$/)return "";out="";for(i=1;i<=length(h);i+=8)for(j=6;j>=0;j-=2)out=out substr(h,i+j,2);k=(length(out)==8?"4" out:(substr(out,1,24)=="00000000000000000000ffff"?"4" substr(out,25):"6" out));return k ":" hex(a[2])}
    function loop(k, a) {split(k,a,":");return(a[1]~/^47f/ || a[1]=="600000000000000000000000000000001")}
    function fields( i,t,a) {
      for(i=1;i<=NF;i++) {t=$i
        if(t~/^uid:[0-9]+$/)owner=substr(t,5)+0
        if(t~/^ino:[0-9]+$/)inode=substr(t,5)
        if(t~/^sk:[0-9a-fA-F]+$/)cookie=substr(t,4)
        if(t~/^bytes_received:[0-9]+$/)received=substr(t,16)+0
        if(t~/^rtt:[0-9.]+\/[0-9.]+$/){split(substr(t,5),a,"/");rtt=a[1]+0}
        if(t~/^lastrcv:[0-9]+$/)age=substr(t,9)+0
      }
    }
    function add(group,key, verified, i) {
      counts[group]++;rxq[group]+=rx;txq[group]+=tx
      if(received>=0){bytes[group]+=received;bytecounts[group]++}
      # ss can omit the receive counter on idle/control loopback sockets. Keep
      # its coverage explicit and invalidate subtotal deltas when it appears.
      if(group=="l")key=key "/b" (received>=0?1:0)
      keys[group]=keys[group] (keys[group]!=""?",":"") key
      if(inode+0<=0 && (cookie=="" || cookie~/^0+$/))unstable[group]++
      if(group=="w") {families[fam]++;if(verified){verified_count++;if(owner==0)root_count++}if(owner<0)unknown_count++
        if(rtt>=0){if(!rtt_n || rtt>rtt_max)rtt_max=rtt;rtt_n++}
        if(age>=0){if(!age_n || age<age_min)age_min=age;age_n++}
      }
    }
    function flush( proc_key,verified,parts,peer,key) {
      if(!selected)return
      selected=0;proc_key=fam SUBSEP inode SUBSEP local_key SUBSEP remote_key
      verified=(rc["p" fam]==0 && ph[fam] && (proc_key in owners))
      if(verified){if(owner>=0 && owner!=owners[proc_key]){verified=0;owner=-1}else owner=owners[proc_key]}
      if(owner==0 && !verified)owner=-1
      key=fam "/" inode "/" cookie "/" local_key "/" remote_key "/" local_zone "/" remote_zone
      split(remote_key,parts,":");peer=parts[1]
      if((peer in neighbors) && (local_zone=="" || local_zone=="wlan2") && (remote_zone=="" || remote_zone=="wlan2"))add("w",key,verified)
      if(uid~/^[0-9]+$/ && owner==uid+0 && loop(local_key) && loop(remote_key))add("l",key,verified)
    }
    function continuity(group,valid,oldvalid,oldbytes,oldkeys, old,i,n,key,changed) {
      changed="na";delta[group]="na";elapsed[group]="na"
      if(valid && oldvalid==1 && !unstable[group]) {
        n=split(oldkeys,old,",");changed=0
        for(i=1;i<=n;i++)if(old[i]!="-")seen[old[i]]=1
        n=split(keys[group],nowkeys,",");for(i=1;i<=n;i++)if(nowkeys[i]!="" && !(nowkeys[i] in seen))changed=1
        n=split(oldkeys,old,",");for(i=1;i<=n;i++)if(old[i]!="-" && index("," keys[group] ",","," old[i] ",")==0)changed=1
        for(key in seen)delete seen[key]
        covered=(group=="l"?(counts[group]==0 || bytecounts[group]>0):bytecounts[group]==counts[group])
        if(!changed && covered && oldbytes!="na" && bytes[group]>=oldbytes+0 && stamp>prior[7]+0){delta[group]=sprintf("%.0f",bytes[group]-oldbytes);elapsed[group]=sprintf("%.0f",stamp-prior[7])}
      }
      topology[group]=changed
    }
    BEGIN {split(previous,prior," ");for(i=1;i<=6;i++)rc[substr("p4p6n4n6s4s6",i*2-1,2)]=-1}
    /^@/ {flush();section=substr($1,2);rc[section]=$2+0;fam=substr(section,2,1);next}
    section~/^p/ {
      if($2=="local_address"){ph[fam]=1;next}
      if(NF>=10 && $8~/^[0-9]+$/ && $10~/^[0-9]+$/ && $10+0>0){a=proc_endpoint($2);b=proc_endpoint($3);if(a!="" && b!="")owners[fam SUBSEP $10 SUBSEP a SUBSEP b]=$8+0}
      next
    }
    section~/^n/ {if($0~/ lladdr / && $NF!="FAILED" && $NF!="INCOMPLETE"){a=ip($1);if(a!="")neighbors[a]=1}next}
    section~/^s/ {
      if($0~/^(State|Netid)[ \t]/){sh[fam]=1;next}
      offset=($1=="tcp" || $1=="tcp6"?1:0)
      if($(offset+1)~/^(ESTAB|LISTEN|UNCONN|SYN-SENT|SYN-RECV|FIN-WAIT-1|FIN-WAIT-2|TIME-WAIT|CLOSE|CLOSE-WAIT|LAST-ACK|CLOSING)$/) {
        flush();if($(offset+1)!="ESTAB")next
        rx=$(offset+2);tx=$(offset+3);local_key=endpoint($(offset+4));remote_key=endpoint($(offset+5))
        local_zone=zone($(offset+4));remote_zone=zone($(offset+5))
        if(rx!~/^[0-9]+$/ || tx!~/^[0-9]+$/ || local_key=="" || remote_key==""){bad[fam]++;next}
        selected=1;owner=-1;inode="";cookie="";received=-1;rtt=-1;age=-1;fields();next
      }
      if(selected && /^[ \t]/){fields();next}
      if(NF && !/^[ \t]/)bad[fam]++
    }
    END {
      flush();sok=(rc["s4"]==0 && rc["s6"]==0 && sh[4] && sh[6] && !bad[4] && !bad[6]);wok=sok && rc["n4"]==0 && rc["n6"]==0;lok=sok && uid~/^[0-9]+$/
      continuity("w",wok,prior[1],prior[2],prior[8]);continuity("l",lok,prior[4],prior[5],prior[9])
      printf "wire_peer_tcp_sockets=%s wire_peer_tcp4_sockets=%s wire_peer_tcp6_sockets=%s wire_peer_proc_verified_sockets=%s wire_peer_root_verified_sockets=%s wire_peer_uid_unknown_sockets=%s",(wok?counts["w"]+0:"na"),(wok?families[4]+0:"na"),(wok?families[6]+0:"na"),(wok?verified_count+0:"na"),(wok?root_count+0:"na"),(wok?unknown_count+0:"na")
      printf " wire_peer_rx_queue_bytes=%s wire_peer_tx_queue_bytes=%s wire_peer_bytes_received_total=%s wire_peer_bytes_received_delta=%s wire_peer_delta_ms=%s wire_peer_topology_changed=%s wire_peer_tcp_rtt_max_ms=%s wire_peer_receive_age_min_ms=%s",(wok?sprintf("%.0f",rxq["w"]):"na"),(wok?sprintf("%.0f",txq["w"]):"na"),(wok && bytecounts["w"]==counts["w"]?sprintf("%.0f",bytes["w"]):"na"),delta["w"],elapsed["w"],topology["w"],(wok && rtt_n?rtt_max:"na"),(wok && age_n?age_min:"na")
      # Loopback totals are the reported-counter subtotal; absent counters are
      # not assumed zero. Coverage and counter availability travel with it.
      lcovered=(counts["l"]==0 || bytecounts["l"]>0)
      printf " zlink_loopback_tcp_sockets=%s zlink_loopback_counter_sockets=%s zlink_loopback_rx_queue_bytes=%s zlink_loopback_tx_queue_bytes=%s zlink_loopback_bytes_received_total=%s zlink_loopback_bytes_received_delta=%s zlink_loopback_delta_ms=%s zlink_loopback_topology_changed=%s",(lok?counts["l"]+0:"na"),(lok?bytecounts["l"]+0:"na"),(lok?sprintf("%.0f",rxq["l"]):"na"),(lok?sprintf("%.0f",txq["l"]):"na"),(lok && lcovered?sprintf("%.0f",bytes["l"]):"na"),delta["l"],elapsed["l"],topology["l"]
      printf "|%d %s 0 %d %s 0 %.0f %s %s",(wok && !unstable["w"]),(wok && bytecounts["w"]==counts["w"]?sprintf("%.0f",bytes["w"]):"na"),(lok && !unstable["l"]),(lok && lcovered?sprintf("%.0f",bytes["l"]):"na"),stamp,(keys["w"]!=""?keys["w"]:"-"),(keys["l"]!=""?keys["l"]:"-")
    }'
}
station_summary() {
  # AP-wide station statistics, not a claim that every station is the phone.
  awk -v ok="$station_ok" '
    /^Station [0-9a-fA-F:]+ \(on wlan2\)/ {n++;selected=1;next}
    /^Station / {selected=0;next}
    selected && $1=="signal:" && $2~/^-?[0-9]+$/ {if(!ns || $2<signal)signal=$2;ns++}
    selected && $1=="rx" && $2=="bitrate:" && $3~/^[0-9.]+$/ && $4=="MBit/s" {if(!nb || $3<bitrate)bitrate=$3;nb++}
    selected && $1=="tx" && $2=="retries:" && $3~/^[0-9]+$/ {retries+=$3;nr++}
    selected && $1=="tx" && $2=="failed:" && $3~/^[0-9]+$/ {failed+=$3;nf++}
    END {
      printf "ap_station_count=%s ap_signal_min_dbm=%s ap_rx_bitrate_min_mbps=%s ap_tx_retries_total=%s ap_tx_failed_total=%s",
        (ok==1?n+0:"na"),(ok==1 && ns?signal:"na"),(ok==1 && nb?bitrate:"na"),
        (ok==1 && nr?sprintf("%.0f",retries):"na"),(ok==1 && nf?sprintf("%.0f",failed):"na")
    }'
}
link_loop() {
  trap - EXIT
  link_seq=0; link_previous=0; link_deadline=$(clock_ms); transport_state=
  while :; do
    link_start=$(clock_ms)
    if [ -r "$FRAME_CONTEXT" ]; then
      { IFS= read -r active; IFS= read -r context_ms; IFS= read -r head; } < "$FRAME_CONTEXT"
      if [ "$active" = 1 ]; then
        link_zpid=$(pidof com.zjinnova.zlink 2>/dev/null | cut -d" " -f1)
        link_uid=na
        [ -n "$link_zpid" ] && link_uid=$(awk "/^Uid:/ {print \$2}" /proc/$link_zpid/status 2>/dev/null)
        neighbour_rows=$(timeout 1 ip -4 neigh show dev wlan2 2>/dev/null); neighbour_rc=$?
        # Keep resolved neighbours only. Failed/absent lookups cannot identify a peer.
        link_peers=$(printf '%s\n' "$neighbour_rows" | awk '$1~/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $0~/ lladdr / && $NF!="FAILED" && $NF!="INCOMPLETE" {printf "%s ",$1}')
        socket_rows=$(timeout 1 ss -4tine 2>/dev/null); socket_rc=$?
        link_ok=0; [ "$neighbour_rc" = 0 ] && [ "$socket_rc" = 0 ] && link_ok=1
        peer_stats=$(printf '%s\n' "$socket_rows" | peer_tcp_summary)
        neighbour6_rows=$(timeout 1 ip -6 neigh show dev wlan2 2>/dev/null); neighbour6_rc=$?
        socket6_rows=$(timeout 1 ss -6tine 2>/dev/null); socket6_rc=$?
        proc4_rows=$(timeout 1 cat /proc/net/tcp 2>/dev/null); proc4_rc=$?
        proc6_rows=$(timeout 1 cat /proc/net/tcp6 2>/dev/null); proc6_rc=$?
        transport_result=$(printf '@p4 %s\n%s\n@p6 %s\n%s\n@n4 %s\n%s\n@n6 %s\n%s\n@s4 %s\n%s\n@s6 %s\n%s\n' \
          "$proc4_rc" "$proc4_rows" "$proc6_rc" "$proc6_rows" \
          "$neighbour_rc" "$neighbour_rows" "$neighbour6_rc" "$neighbour6_rows" \
          "$socket_rc" "$socket_rows" "$socket6_rc" "$socket6_rows" | transport_v6_summary)
        # Android mksh treats an unescaped pipe as pattern alternation here.
        transport_stats=${transport_result%%\|*}; transport_state=${transport_result#*\|}
        station_rows=$(timeout 1 iw dev wlan2 station dump 2>/dev/null); station_rc=$?
        station_ok=0; [ "$station_rc" = 0 ] && station_ok=1
        station_stats=$(printf '%s\n' "$station_rows" | station_summary)
        link_gap=na; [ "$link_previous" -gt 0 ] && link_gap=$((link_start-link_previous))
        link_previous=$link_start; link_seq=$((link_seq+1))
        # The full diagnostic context is emitted separately. Keep this event below
        # the retained-message limit so its transport tail survives file recovery.
        link_acc=$(printf '%s\n' "$head" | awk '{for(i=1;i<=NF;i++)if($i~/^acc=[01]$/){print $i;exit}}')
        link_head="session=$SESSION schema=6 ${link_acc:-acc=na}"
        message="sample=$SESSION-link-$link_seq $link_head | event=wireless_link link_poll_gap_ms=$link_gap link_probe_ms=$(($(clock_ms)-link_start)) link_context_age_ms=$((link_start-context_ms)) $peer_stats $station_stats $transport_stats"
        printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$message" >> "$LOG"
        log -p "$PRIO" -t "$TAG" "$message" 2>/dev/null
        unset socket_rows socket6_rows neighbour_rows neighbour6_rows proc4_rows proc6_rows station_rows link_peers transport_result
      else
        link_previous=0; transport_state=
      fi
    else
      link_previous=0; transport_state=
    fi
    set -- $(deadline_delay "$link_deadline" "$(clock_ms)" 3000)
    link_deadline=$1
    sleep "$2"
  done
}
surface_queue_summary() {
  awk '
    /^\+ Layer / {selected=($0~/^\+ Layer \(com\.zjinnova\.zlink\/[A-Za-z0-9_.$]+#[0-9]+\) uid=[0-9]+$/)}
    selected && /queued-frames=/ {
      for(i=1;i<=NF;i++)if($i~/^queued-frames=[0-9]+$/) {
        v=substr($i,15)+0;if(!n || v>max)max=v;n++
      }
    }
    END {printf "zlink_queued_layers=%s zlink_queued_frames_max=%s",(n?n:"na"),(n?max:"na")}'
}
# These parsers emit allowlisted aggregates only. Never persist raw dumps. Codec records
# can be published after disconnect: the source local timestamp is NOT collection time.
codec_summary() {
  awk '
    /^[ \t]*[0-9]+: \{codec, \([^)]*\), \(com\.zjinnova\.zlink, / {
      for(k in v)delete v[k]
      n=split($0,parts,", ")
      for(i=1;i<=n;i++) {
        p=parts[i];sub(/^\(/,"",p);split(p,kv,"=")
        if(index(kv[1],"android.media.mediacodec.")==1) {
          key=kv[1];sub(/^android\.media\.mediacodec\./,"",key)
          value=kv[2];sub(/[)}]+$/,"",value);v[key]=value
        }
      }
      if(v["encoder"]!="0" || v["mime"]!~/^video\//)next
      stamp=parts[2];gsub(/[()]/,"",stamp);gsub(/ /,"_",stamp)
      if(stamp!~/^[0-9][0-9]-[0-9][0-9]_[0-9][0-9]:[0-9][0-9]:[0-9][0-9]\.[0-9]+$/)next
      out="event=codec_summary codec_reported_local=" stamp
      count=split("latency.avg latency.max latency.min latency.n lifetimeMs low-latency.on low-latency.off width height profile level flush-count resolution-change-count set-surface-count used-max-input-size",keys," ")
      split("codec_latency_avg_us codec_latency_max_us codec_latency_min_us codec_latency_n codec_lifetime_ms codec_low_latency_on codec_low_latency_off codec_width codec_height codec_profile codec_level codec_flush_count codec_resolution_change_count codec_set_surface_count codec_used_max_input_size",names," ")
      for(i=1;i<=count;i++)out=out " " names[i] "=" (v[keys[i]]~/^[0-9]+$/ ? v[keys[i]] : "na")
      print out
    }'
}
graphics_summary() {
  awk '
    /^\*\* Graphics info for pid [0-9]+ \[com\.zjinnova\.zlink\] \*\*$/ {owner=1;next}
    /^\*\* Graphics info/ {owner=0}
    owner && /^Stats since: [0-9]+ns$/ {v["gfx_since_ns"]=$3;sub(/ns$/,"",v["gfx_since_ns"])}
    owner && /^Total frames rendered: [0-9]+$/ {v["gfx_frames"]=$4}
    owner && /^Janky frames: [0-9]+ / {v["gfx_janky"]=$3}
    owner && /^95th percentile: [0-9]+ms$/ {v["gfx_p95_ms"]=$3;sub(/ms$/,"",v["gfx_p95_ms"])}
    owner && /^Number High input latency: [0-9]+$/ {v["gfx_high_input_latency"]=$5}
    owner && /^Number Slow UI thread: [0-9]+$/ {v["gfx_slow_ui_thread"]=$5}
    END {
      n=split("gfx_since_ns gfx_frames gfx_janky gfx_p95_ms gfx_high_input_latency gfx_slow_ui_thread",keys," ")
      printf "event=graphics_summary"
      for(i=1;i<=n;i++)printf " %s=%s",keys[i],(v[keys[i]]~/^[0-9]+$/ ? v[keys[i]] : "na")
      print ""
    }'
}
network_summary() {
  awk '
    $1=="Tcp:" || $1=="Udp:" {
      proto=$1
      if($2!~/^[0-9]+$/) {for(i=2;i<=NF;i++)header[proto,i]=$i;next}
      for(i=2;i<=NF;i++)if($i~/^[0-9]+$/)v[proto header[proto,i]]=$i
    }
    END {
      split("Tcp:RetransSegs Udp:RcvbufErrors Udp:SndbufErrors",keys," ")
      split("device_tcp_retrans_segs device_udp_rcvbuf_errors device_udp_sndbuf_errors",names," ")
      printf "event=network_summary"
      for(i=1;i<=3;i++)printf " %s=%s",names[i],(v[keys[i]]~/^[0-9]+$/ ? v[keys[i]] : "na")
      print ""
    }'
}
slow_diagnostics() {
  trap - EXIT
  # Background subshell: independent IDs avoid racing the foreground pipeline counter.
  # Only one worker per sampler; each Binder dump has a two-second service timeout.
  capture=$(date +%s); slow_seq=0
  cache=/data/local/tmp/.dashcam_cpt_codec_snapshot
  fresh="$cache.$SESSION"
  dumpsys -t 2 media.metrics 2>/dev/null | codec_summary | tail -8 > "$fresh"
  while IFS= read -r row; do
    grep -Fqx "$row" "$cache" 2>/dev/null || emit_slow "$row"
  done < "$fresh"
  # A failed/empty dump must not forget previous summaries and re-emit them next time.
  if [ -s "$fresh" ]; then mv "$fresh" "$cache"; else rm -f "$fresh"; fi
  row=$(dumpsys -t 2 gfxinfo com.zjinnova.zlink 2>/dev/null | graphics_summary)
  emit_slow "$row"
  row=$(network_summary < /proc/net/snmp 2>/dev/null)
  emit_slow "$row"
}
emit_slow() {
  slow_seq=$((slow_seq+1))
  message="sample=$SESSION-diag-$capture-$slow_seq session=$SESSION schema=6 | $1"
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$message" >> "$LOG"
  log -p "$PRIO" -t "$TAG" "$message" 2>/dev/null
}
emit() {
  # SurfaceFlinger pipelines run their loop bodies in subshells.  Keep the counter in a
  # tiny file so every emission, including one from a pipeline, receives a unique ID.
  seq=$(cat "$SEQF" 2>/dev/null)
  case "$seq" in ''|*[!0-9]*) seq=0;; esac
  seq=$((seq + 1))
  echo "$seq" > "$SEQF"
  message="sample=$SESSION-$seq $1"
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $message" >> "$LOG"
  log -p "$PRIO" -t "$TAG" "$message" 2>/dev/null
}
rotate_log() {
  # Keep current + eight older files, rotated before the next context pass. Do not
  # truncate an open file: rename lets the next append create a clean generation.
  [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null)" -ge $((LOG_KIB * 1024)) ] || return
  rm -f "$LOG.$LOG_ROTATIONS"
  n=$LOG_ROTATIONS
  while [ "$n" -gt 1 ]; do
    old=$((n - 1)); [ -f "$LOG.$old" ] && mv "$LOG.$old" "$LOG.$n"
    n=$old
  done
  mv "$LOG" "$LOG.1"
}

sample_surfaces() {
    # Live owner/parent mapping found the anonymous BLAST layers under the camera
    # recorder. Only select the Zlink application buffer name, excluding window tokens,
    # ActivityRecords, input sinks and hash-prefixed window containers. Fail closed when
    # the vendor naming changes. Raw titles never leave this local latency query.
    layers=$(dumpsys -t 1 SurfaceFlinger --list 2>/dev/null | awk '
      /^com\.zjinnova\.zlink\/[A-Za-z0-9_.$]+#[0-9]+$/ { print "package_window\t" $0 }
    ')
    if [ -z "$layers" ]; then
      emit "$head | no video surface"
    else
      idx=0
      echo "$layers" | while IFS='	' read -r kind L; do
        idx=$((idx+1))
        # Never log a full window title: retain only its numeric SurfaceFlinger ID.
        id=$(echo "$L" | sed -n 's/.*#\([0-9][0-9]*\)$/#\1/p')
        [ -n "$id" ] || continue
        mark="$MARKPFX${id#\#}"
        seen=$(cat "$mark" 2>/dev/null)
        dumpsys -t 1 SurfaceFlinger --latency "$L" </dev/null 2>/dev/null | awk -v layer="$id" -v kind="$kind" -v idx="$idx" -v seen="${seen:-0}" -v poll_ms="$poll_ms" -v mark="$mark" '
          NR==1 { period=$1/1e6; next }
          NF>=3 && $2>0 && $2<9e18 {
            p[n++]=$2
            # Column 3 is frame-ready; column 2 is actual presentation. This measures
            # local ready-to-present delay, NOT phone-to-display or touch latency.
            if ($3>0 && $3<9e18 && $2>=$3) ready[sprintf("%.0f",$2)]=($2-$3)/1e6
          }
          END {
            if (n<3) { printf "layer=%s surface_kind=%s idx=%s frames=%d (idle)\n", layer, kind, idx, n; exit }
            # sort presented timestamps, then the intervals between them
            for (i=0;i<n;i++) for (j=i+1;j<n;j++) if (p[j]<p[i]) { t=p[i]; p[i]=p[j]; p[j]=t }
            # Where this window ended, for the next one to start after. Printed with an
            # explicit integer format: these are nanosecond timestamps, and awk`s default
            # output would render them in exponent form, which reads back as a different
            # number and would silently disable the de-duplication entirely.
            printf "%.0f\n", p[n-1] > mark
            # Only intervals whose later frame is new. Consecutive reads of a 5.3 s ring
            # four seconds apart share about a second of frames, and counting those twice
            # would inflate every hold count by the overlap. The interval that straddles
            # the boundary belongs to this window and is kept exactly once.
            # SurfaceFlinger timestamps restart after a reboot.  A persisted marker from
            # before that restart must not discard every initial frame of the new session.
            if (p[n-1] < seen+0) seen=0
            overlap="na"; ring_gap="na"
            if(seen>0) {overlap=(p[0]<=seen ? 1 : 0);ring_gap=sprintf("%.1f",(p[0]>seen ? (p[0]-seen)/1e6 : 0))}
            unchanged="na"
            if(poll_ms>0) {
              freshfile=mark ".fresh";getline last_fresh < freshfile;close(freshfile)
              if(p[n-1]>seen || !last_fresh || last_fresh>poll_ms) {
                last_fresh=poll_ms;printf "%.0f\n",poll_ms > freshfile;close(freshfile)
              }
              unchanged=sprintf("%.0f",poll_ms-last_fresh)
            }
            m=0; rn=0
            for (i=1;i<n;i++) if (p[i] > seen+0) {
              key=sprintf("%.0f",p[i]); if(key in ready) rdelay[rn++]=ready[key]
              d[m]=(p[i]-p[i-1])/1e6
              if (m==0) first=p[i-1]
              last=p[i]; m++
            }
            if (m<2) { printf "layer=%s surface_kind=%s idx=%s frames=%d new=0 ring_overlap=%s ring_gap_ms=%s surface_unchanged_ms=%s (no new frames)\n", layer, kind, idx, n, overlap, ring_gap, unchanged; exit }
            span=(last-first)/1e9
            for (i=0;i<m;i++) for (j=i+1;j<m;j++) if (d[j]<d[i]) { t=d[i]; d[i]=d[j]; d[j]=t }
            for (i=0;i<rn;i++) for(j=i+1;j<rn;j++) if(rdelay[j]<rdelay[i]) {t=rdelay[i];rdelay[i]=rdelay[j];rdelay[j]=t}
            rp95="na"; rmax="na"
            if(rn>0) {ri=int(rn*0.95);if(ri<rn*0.95)ri++;rp95=sprintf("%.1f",rdelay[ri-1]);rmax=sprintf("%.1f",rdelay[rn-1])}
            med=d[int(m/2)]
            late=0; hitch=0
            # A late frame is one that missed its slot, and the slot is this surface`s own
            # cadence -- not the display`s. The old threshold was 2.5 display periods, which
            # on a 57 Hz panel is 44 ms; a 30 fps source cannot be shown evenly there and has
            # to alternate 2-vsync (35 ms) and 3-vsync (53 ms) holds, so every ordinary
            # 3-vsync hold counted late. Worse, a surface running steadily at 19 fps scored
            # 100% late while dropping nothing at all. Measuring from the median instead
            # separates the two questions the fields already answer separately: fps says how
            # fast the surface runs, late says how unevenly.
            thr=med+1.5*period
            # A hitch is a hold long enough to see, which is a different question again.
            # Across a day of driving `late` sat at a median of 11% while the worst single
            # hold reached 265 ms -- a quarter of a second of frozen picture that `late`
            # scored 26%, because one long hold among many even ones barely moves a rate.
            # Twice the median is the surface`s own definition of "stopped for a moment",
            # so it needs no panel constant and follows the link if its cadence changes.
            hthr=2*med
            for (i=0;i<m;i++) { if (d[i]>thr) late++; if (d[i]>=hthr) hitch++ }
            printf "layer=%s surface_kind=%s idx=%s fps=%.1f med=%.1f p95=%.1f max=%.1f late=%d%% hitch=%d n=%d new=%d span=%.1f period=%.1f thr=%.1f ready_n=%d ready_p95=%s ready_max=%s ring_overlap=%s ring_gap_ms=%s surface_unchanged_ms=%s\n",
              layer, kind, idx, (span>0? m/span:0), med, d[int(m*0.95)-1<0?0:int(m*0.95)-1], d[m-1], 100*late/m, hitch, n, m, span, period, thr, rn, rp95, rmax, overlap, ring_gap, unchanged
          }' | while read -r stat; do emit "$head | $stat"; done
      done
    fi
}
frame_loop() {
  # Independent sequence file: emit() is also used from pipeline subshells.
  SEQF=$FRAME_SEQ
  SESSION="$SESSION-frame-$(clock_ms)"
  trap - EXIT
  frame_pid=; slow_pid=
  echo 0 > "$SEQF"
  frame_deadline=$(clock_ms); previous_poll=0
  while :; do
    poll_ms=$(clock_ms)
    if [ -r "$FRAME_CONTEXT" ]; then
      { IFS= read -r active; IFS= read -r context_ms; IFS= read -r head; } < "$FRAME_CONTEXT"
      if [ "$active" = 1 ]; then
        gap=na; [ "$previous_poll" -gt 0 ] && gap=$((poll_ms-previous_poll))
        head="$head context_age_ms=$((poll_ms-context_ms)) frame_poll_gap_ms=$gap"
        sample_surfaces
        previous_poll=$poll_ms
      else
        previous_poll=0
      fi
    fi
    # Preserve the deadline across iterations; work is part of the period.
    set -- $(deadline_delay "$frame_deadline" "$(clock_ms)" "$((FRAME_INTERVAL*1000))")
    frame_deadline=$1
    sleep "$2"
  done
}

while :; do
  rotate_log
  context_ms=$(clock_ms)
  now=$(date +%s)
  acc=$(settings get global acc_status 2>/dev/null)
  # Aggregate reachability states only.  A neighbour is useful sampling context, but it
  # does not identify a phone and cannot prove that CarPlay is active.
  neighbour=$(ip neigh show dev wlan2 2>/dev/null | awk '
    $NF == "REACHABLE" { r++ }
    $NF == "STALE" { s++ }
    $NF == "DELAY" { d++ }
    $NF == "PROBE" { p++ }
    $NF == "PERMANENT" { m++ }
    END { printf "r:%d,s:%d,d:%d,p:%d,m:%d,count:%d", r+0,s+0,d+0,p+0,m+0,r+s+d+p+m }
  ')
  phone=${neighbour##*,count:}
  neigh=${neighbour%,count:*}
  load=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)
  soc=na
  for z in /sys/class/thermal/thermal_zone*; do
    [ "$(cat $z/type 2>/dev/null)" = "soc-thmzone" ] && soc=$(awk '{printf "%.1f",$1/1000}' $z/temp 2>/dev/null)
  done
  # Zlink CPU% over the interval from /proc/<pid>/stat (utime+stime, 100 ticks/s).
  zpid=$(pidof com.zjinnova.zlink 2>/dev/null | cut -d' ' -f1)
  zcpu=na
  if [ -n "$zpid" ] && [ -r /proc/$zpid/stat ]; then
    ticks=$(awk '{print $14+$15}' /proc/$zpid/stat 2>/dev/null)
    if [ "$prev_zpid" = "$zpid" ] && [ "$prev_t" -gt 0 ] && [ -n "$ticks" ] && [ "$ticks" -ge "$prev_ticks" ]; then
      dt=$((now-prev_t)); [ "$dt" -gt 0 ] && zcpu=$(( (ticks-prev_ticks) / dt ))
    fi
    prev_ticks=${ticks:-0}; prev_t=$now; prev_zpid=$zpid
  else
    prev_ticks=0; prev_t=0; prev_zpid=
  fi
  # Hotspot incoming bitrate (phone -> unit = the CarPlay picture).
  rx=$(cat /sys/class/net/wlan2/statistics/rx_bytes 2>/dev/null)
  kbit=na
  if [ -n "$rx" ] && [ "$prev_rx_t" -gt 0 ] && [ "$rx" -ge "$prev_rx" ]; then
    rdt=$((now-prev_rx_t)); [ "$rdt" -gt 0 ] && kbit=$(( (rx-prev_rx)*8/rdt/1000 ))
  fi
  if [ -n "$rx" ]; then
    prev_rx=$rx; prev_rx_t=$now
  else
    prev_rx=0; prev_rx_t=0
  fi
  sta=$(timeout 2 cmd wifi status 2>/dev/null | grep -oE 'Frequency: [0-9]+MHz|RSSI: -?[0-9]+' | head -2 | tr -d ' ' | tr '\n' '/' )
  sta_mhz=$(echo "$sta" | sed -n 's/.*Frequency:\([0-9][0-9]*\)MHz.*/\1/p')
  [ -n "$sta_mhz" ] || sta_mhz=na
  ap=$(dumpsys -t 2 wifi 2>/dev/null | grep -oE 'wlan2=SoftApInfo\{[^}]*frequency= [0-9]+' | grep -oE '[0-9]+$' | head -1)
  # What the hotspot link *lost*, not just what it carried. Dropped and errored frames on
  # the AP are the direct evidence of a link that stalled, which average bitrate cannot
  # show: measured across 234 samples rx_kbit correlates with late frames at r=-0.00, yet a
  # picture that visibly stutters is plainly losing something. Standard netdev counters, so
  # they are always present; the delta is what matters, not the total.
  drops=na
  d_now=$(cat /sys/class/net/wlan2/statistics/tx_dropped 2>/dev/null)
  e_now=$(cat /sys/class/net/wlan2/statistics/tx_errors 2>/dev/null)
  r_now=$(cat /sys/class/net/wlan2/statistics/rx_dropped 2>/dev/null)
  if [ -n "$d_now" ] && [ -n "$e_now" ] && [ -n "$r_now" ]; then
    total=$((d_now + e_now + r_now))
    # -1 until the first reading, because a healthy link legitimately sits at zero: using
    # the total itself as the sentinel reported `na` -- unreadable -- for exactly the case
    # the column exists to recognise, and the live unit does read 0/0/0.
    [ "$prev_drops" -ge 0 ] && [ "$total" -ge "$prev_drops" ] && drops=$((total - prev_drops))
    prev_drops=$total
  else
    prev_drops=-1
  fi
  # The OBD logger's own CPU, read exactly as Zlink's is. It polls the car over BLE while
  # driving -- at a duty cycle its own event stream has reported as high as 100% -- and
  # Bluetooth shares this unit's single radio with the hotspot CarPlay runs over (see the
  # opening note in app/ingest/radios.py). If that coexistence costs frames, this is the
  # column that will show it. Until it is recorded the question cannot be settled at all:
  # the logger polls whenever the engine runs and CarPlay samples exist only when the
  # engine runs, so the data collected so far has no contrast to correlate against.
  opid=$(pidof com.dashcamstats.obdlogger 2>/dev/null | cut -d' ' -f1)
  ocpu=na
  if [ -n "$opid" ] && [ -r /proc/$opid/stat ]; then
    oticks=$(awk '{print $14+$15}' /proc/$opid/stat 2>/dev/null)
    if [ "$prev_opid" = "$opid" ] && [ "$prev_ot" -gt 0 ] && [ -n "$oticks" ] && [ "$oticks" -ge "$prev_oticks" ]; then
      odt=$((now-prev_ot)); [ "$odt" -gt 0 ] && ocpu=$(( (oticks-prev_oticks) / odt ))
    fi
    prev_oticks=${oticks:-0}; prev_ot=$now; prev_opid=$opid
  else
    prev_oticks=0; prev_ot=0; prev_opid=
  fi
  bt=$(settings get global bluetooth_on 2>/dev/null)
  zlink_proc=0; [ -n "$zpid" ] && zlink_proc=1
  diag="schema=6"
  if [ "$phone" -gt 0 ] || { [ "$acc" = 1 ] && [ -n "$zpid" ]; }; then
    diagnostic_context
  fi
  head="session=$SESSION acc=$acc phone=$phone neigh_count=$phone neigh=$neigh load=$load soc=$soc zlink_proc=$zlink_proc zlink_cpu=$zcpu rx_kbit=$kbit ap_drops=$drops obd_cpu=$ocpu bt=${bt:-na} sta=$sta ap=${ap:-na} $diag"

  [ "$acc" = 1 ] || [ "$phone" -gt 0 ] && last_active=$now
  # First pass recovers retained summaries, then once/minute during activity and for
  # two minutes afterwards so decoder teardown records survive an offline departure.
  if { [ "$slow_last" -eq 0 ] || [ $((now-last_active)) -le 120 ]; } && [ $((now-slow_last)) -ge 60 ]; then
    if ! child_alive "$slow_pid" "$slow_start"; then
      slow_diagnostics &
      slow_pid=$!; slow_last=$now
      slow_start=$(process_start "$slow_pid")
    fi
  fi

  [ "$started" -eq 0 ] && emit "$head | event=sampler_started" && started=1
  if [ "$prev_neigh" != na ] && [ "$phone" != "$prev_neigh" ]; then
    [ "$phone" -gt 0 ] && event=hotspot_neighbour_present || event=hotspot_neighbour_absent
    emit "$head | event=$event"
  fi
  if [ "$prev_sta_mhz" != na ] && { [ "$sta_mhz" != "$prev_sta_mhz" ] || [ "${ap:-na}" != "$prev_ap" ]; }; then
    emit "$head | event=radio_frequency_changed"
  fi
  prev_neigh=$phone; prev_sta_mhz=$sta_mhz; prev_ap=${ap:-na}

  if [ "$phone" -gt 0 ] || { [ "$acc" = 1 ] && [ -n "$zpid" ]; }; then
    # Preserve pressure/queue evidence even when no display layer is available.
    emit "$head | event=diagnostic_context"
    idle_n=0
  else
    idle_n=$((idle_n+1))
    # A heartbeat once a minute while no phone is attached: enough to prove it is alive.
    [ $((idle_n % 4)) -eq 1 ] && emit "$head | no phone on hotspot"
  fi
  if [ "$phone" -gt 0 ] || { [ "$acc" = 1 ] && [ -n "$zpid" ]; }; then active=1; else active=0; fi
  printf '%s\n%s\n%s\n' "$active" "$context_ms" "$head" > "$FRAME_CONTEXT.new"
  mv "$FRAME_CONTEXT.new" "$FRAME_CONTEXT"
  # Independent five-second codec windows, at most once/minute. These are local elapsed
  # codec measurements, not phone-side content age. The helper guards other tracers and
  # reports skips/errors explicitly, with bounded cleanup on sampler replacement.
  if [ -n "$codec_pid" ] && ! codec_alive; then
    wait "$codec_pid" 2>/dev/null
    codec_pid=; codec_start=
  fi
  if [ "$active" = 1 ] && [ -n "$zpid" ] && [ $((now-codec_last)) -ge 60 ] && [ -r "$CODEC_SCRIPT" ]; then
    if [ -z "$codec_pid" ]; then
      codec_seq=$((codec_seq+1)); codec_last=$now
      sh "$CODEC_SCRIPT" "$SESSION" "$codec_seq" "$LOG" "$PRIO" &
      codec_pid=$!
      codec_start=$(process_start "$codec_pid")
    fi
  fi
  if ! child_alive "$frame_pid" "$frame_start"; then
    frame_loop &
    frame_pid=$!
    frame_start=$(process_start "$frame_pid")
  fi
  if ! child_alive "$link_pid" "$link_start_ticks"; then
    link_loop &
    link_pid=$!
    link_start_ticks=$(process_start "$link_pid")
  fi
  sleep "$INTERVAL" &
  sleep_pid=$!
  sleep_start=$(process_start "$sleep_pid")
  wait "$sleep_pid"
  sleep_pid=
done

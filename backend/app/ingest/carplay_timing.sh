#!/system/bin/sh
# dashcam-stats CarPlay timing sampler. Deleting this file, its .pid and its .log removes
# every trace. Read-only: it never changes a setting, a radio, or a process.
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
SEQF=/data/local/tmp/.dashcam_carplay_timing.seq
if [ -f "$PIDF" ]; then
  oldpid=$(cat "$PIDF" 2>/dev/null)
  case "$oldpid" in
    ''|*[!0-9]*) ;;
    *) [ -r "/proc/$oldpid/cmdline" ] && tr '\000' ' ' < "/proc/$oldpid/cmdline" | grep -q "$SCRIPT" && kill "$oldpid" 2>/dev/null ;;
  esac
fi
echo $$ > "$PIDF"
echo 0 > "$SEQF"

prev_ticks=0; prev_t=0; prev_zpid=; prev_rx=0; prev_rx_t=0; idle_n=0; prev_drops=-1
prev_oticks=0; prev_ot=0; prev_opid=; prev_neigh=na; prev_sta_mhz=na; prev_ap=na; started=0
prev_cticks=0; prev_ct=0; prev_cpid=
slow_pid=; slow_last=0; last_active=0
FRAME_CONTEXT=/data/local/tmp/.dashcam_cpt_context_$SESSION
FRAME_SEQ=/data/local/tmp/.dashcam_cpt_frame_seq_$SESSION
frame_pid=
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
  [ -n "$frame_pid" ] && kill "$frame_pid" 2>/dev/null
  [ -n "$slow_pid" ] && kill "$slow_pid" 2>/dev/null
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
  diag="schema=4 mem_available_kib=${mem:-na} cpu_pressure=${pcpu:-na} io_pressure=${pio:-na} memory_pressure=${pmem:-na} $clocks zlink_rss_kib=${rss:-na} zlink_threads=${threads:-na} $queues decoder_cpu=${ccpu:-na} $transport $display_queue $sched"
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
      count=split("latency.avg latency.max latency.min latency.n lifetimeMs low-latency.on low-latency.off",keys," ")
      split("codec_latency_avg_us codec_latency_max_us codec_latency_min_us codec_latency_n codec_lifetime_ms codec_low_latency_on codec_low_latency_off",names," ")
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
  message="sample=$SESSION-diag-$capture-$slow_seq session=$SESSION schema=4 | $1"
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
  sta=$(cmd wifi status 2>/dev/null | grep -oE 'Frequency: [0-9]+MHz|RSSI: -?[0-9]+' | head -2 | tr -d ' ' | tr '\n' '/' )
  sta_mhz=$(echo "$sta" | sed -n 's/.*Frequency:\([0-9][0-9]*\)MHz.*/\1/p')
  [ -n "$sta_mhz" ] || sta_mhz=na
  ap=$(dumpsys wifi 2>/dev/null | grep -oE 'wlan2=SoftApInfo\{[^}]*frequency= [0-9]+' | grep -oE '[0-9]+$' | head -1)
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
  diag="schema=4"
  if [ "$phone" -gt 0 ] || { [ "$acc" = 1 ] && [ -n "$zpid" ]; }; then
    diagnostic_context
  fi
  head="session=$SESSION acc=$acc phone=$phone neigh_count=$phone neigh=$neigh load=$load soc=$soc zlink_proc=$zlink_proc zlink_cpu=$zcpu rx_kbit=$kbit ap_drops=$drops obd_cpu=$ocpu bt=${bt:-na} sta=$sta ap=${ap:-na} $diag"

  [ "$acc" = 1 ] || [ "$phone" -gt 0 ] && last_active=$now
  # First pass recovers retained summaries, then once/minute during activity and for
  # two minutes afterwards so decoder teardown records survive an offline departure.
  if { [ "$slow_last" -eq 0 ] || [ $((now-last_active)) -le 120 ]; } && [ $((now-slow_last)) -ge 60 ]; then
    if [ -z "$slow_pid" ] || ! kill -0 "$slow_pid" 2>/dev/null; then
      slow_diagnostics &
      slow_pid=$!; slow_last=$now
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
  if [ -z "$frame_pid" ] || ! kill -0 "$frame_pid" 2>/dev/null; then
    frame_loop &
    frame_pid=$!
  fi
  sleep "$INTERVAL"
done

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
# How often the video surfaces themselves are read, as against the context around them.
#
# SurfaceFlinger keeps a ring of 127 frames per layer, which at 24 presentations per second
# is about 5.3 seconds. Reading it once every INTERVAL seconds therefore observed 5.3 s
# out of every 15 at that cadence -- a 36% duty cycle, with 64% never looked at, which is
# enough for a two-second stutter to be missed better than half the time. At 4 s the
# windows overlap instead of leaving gaps (and stay inside the ring even at 28 fps, where
# it holds 4.5 s). The overlap is then removed per layer by MARKPFX below, so a frame is
# never counted twice. The context reads above keep the slow cadence: they are the
# expensive part, and load and temperature do not move in four seconds.
FRAME_INTERVAL="${3:-4}"
MARKPFX=/data/local/tmp/.dashcam_cpt_seen_
LOG=/data/local/tmp/dashcam_carplay_timing.log
PIDF=/data/local/tmp/.dashcam_carplay_timing.pid
TAG=CarPlayTiming
# Bounded direct-file history is independent from logcat's small, noisy buffers.  It is
# read non-destructively and deduplicated when the unit returns home.
LOG_KIB=512
LOG_ROTATIONS=6
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
  # Keep current + six older files, each capped before the next context pass.  Do not
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

while :; do
  rotate_log
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
  head="session=$SESSION acc=$acc phone=$phone neigh_count=$phone neigh=$neigh load=$load soc=$soc zlink_proc=$zlink_proc zlink_cpu=$zcpu rx_kbit=$kbit ap_drops=$drops obd_cpu=$ocpu bt=${bt:-na} sta=$sta ap=${ap:-na}"

  [ "$started" -eq 0 ] && emit "$head | event=sampler_started" && started=1
  if [ "$prev_neigh" != na ] && [ "$phone" != "$prev_neigh" ]; then
    [ "$phone" -gt 0 ] && event=hotspot_neighbour_present || event=hotspot_neighbour_absent
    emit "$head | event=$event"
  fi
  if [ "$prev_sta_mhz" != na ] && { [ "$sta_mhz" != "$prev_sta_mhz" ] || [ "${ap:-na}" != "$prev_ap" ]; }; then
    emit "$head | event=radio_frequency_changed"
  fi
  prev_neigh=$phone; prev_sta_mhz=$sta_mhz; prev_ap=${ap:-na}

  if [ "$phone" -gt 0 ]; then
    idle_n=0
    # One context reading, several surface readings under it -- see FRAME_INTERVAL.
    watched=0
    while [ "$watched" -lt "$INTERVAL" ]; do
    # Live owner/parent mapping found the anonymous BLAST layers under the camera
    # recorder. Only select the Zlink application buffer name, excluding window tokens,
    # ActivityRecords, input sinks and hash-prefixed window containers. Fail closed when
    # the vendor naming changes. Raw titles never leave this local latency query.
    layers=$(dumpsys SurfaceFlinger --list 2>/dev/null | awk '
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
        dumpsys SurfaceFlinger --latency "$L" </dev/null 2>/dev/null | awk -v layer="$id" -v kind="$kind" -v idx="$idx" -v seen="${seen:-0}" -v mark="$mark" '
          NR==1 { period=$1/1e6; next }
          NF>=3 && $2>0 && $2<9e18 { p[n++]=$2 }
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
            m=0
            for (i=1;i<n;i++) if (p[i] > seen+0) {
              d[m]=(p[i]-p[i-1])/1e6
              if (m==0) first=p[i-1]
              last=p[i]; m++
            }
            if (m<2) { printf "layer=%s surface_kind=%s idx=%s frames=%d new=0 (no new frames)\n", layer, kind, idx, n; exit }
            span=(last-first)/1e9
            for (i=0;i<m;i++) for (j=i+1;j<m;j++) if (d[j]<d[i]) { t=d[i]; d[i]=d[j]; d[j]=t }
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
            printf "layer=%s surface_kind=%s idx=%s fps=%.1f med=%.1f p95=%.1f max=%.1f late=%d%% hitch=%d n=%d new=%d span=%.1f period=%.1f thr=%.1f\n",
              layer, kind, idx, (span>0? m/span:0), med, d[int(m*0.95)-1<0?0:int(m*0.95)-1], d[m-1], 100*late/m, hitch, n, m, span, period, thr
          }' | while read -r stat; do emit "$head | $stat"; done
      done
    fi
    sleep "$FRAME_INTERVAL"
    watched=$((watched + FRAME_INTERVAL))
    done
  else
    idle_n=$((idle_n+1))
    # A heartbeat once a minute while no phone is attached: enough to prove it is alive.
    [ $((idle_n % 4)) -eq 1 ] && emit "$head | no phone on hotspot"
    sleep "$INTERVAL"
  fi
done

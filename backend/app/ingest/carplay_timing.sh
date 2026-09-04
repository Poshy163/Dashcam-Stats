#!/system/bin/sh
# dashcam-stats CarPlay timing sampler. Deleting this file, its .pid and its .log removes
# every trace. Read-only: it never changes a setting, a radio, or a process.
#
# Every INTERVAL seconds, while a phone is attached to the unit's CarPlay hotspot, it reads
# the frame timing of Zlink's video surfaces straight from SurfaceFlinger -- the path that
# carries the CarPlay picture, which Zlink's own gfxinfo does not cover -- and alongside it
# the things that could be starving that path: load, SoC temperature, Zlink's own CPU, the
# hotspot's incoming bitrate, and which channels the two radio roles are on. One line per
# surface goes to the log file and, at priority PRIO, into logcat under the tag
# CarPlayTiming, so the unit-log collector ships it home on the next visit.
INTERVAL="${1:-15}"
PRIO="${2:-w}"
LOG=/data/local/tmp/dashcam_carplay_timing.log
PIDF=/data/local/tmp/.dashcam_carplay_timing.pid
TAG=CarPlayTiming
[ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
echo $$ > "$PIDF"

prev_ticks=0; prev_t=0; prev_rx=0; idle_n=0; prev_drops=0; prev_oticks=0; prev_ot=0
emit() {
  echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $1" >> "$LOG"
  log -p "$PRIO" -t "$TAG" "$1" 2>/dev/null
}

while :; do
  now=$(date +%s)
  acc=$(settings get global acc_status 2>/dev/null)
  phone=$(ip neigh show dev wlan2 2>/dev/null | grep -c REACHABLE)
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
    if [ "$prev_t" -gt 0 ] && [ -n "$ticks" ]; then
      dt=$((now-prev_t)); [ "$dt" -gt 0 ] && zcpu=$(( (ticks-prev_ticks) / dt ))
    fi
    prev_ticks=${ticks:-0}; prev_t=$now
  fi
  # Hotspot incoming bitrate (phone -> unit = the CarPlay picture).
  rx=$(cat /sys/class/net/wlan2/statistics/rx_bytes 2>/dev/null)
  kbit=na
  if [ -n "$rx" ] && [ "$prev_rx" -gt 0 ]; then
    kbit=$(( (rx-prev_rx)*8/INTERVAL/1000 ))
  fi
  prev_rx=${rx:-0}
  sta=$(cmd wifi status 2>/dev/null | grep -oE 'Frequency: [0-9]+MHz|RSSI: -?[0-9]+' | head -2 | tr -d ' ' | tr '\n' '/' )
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
    [ "$prev_drops" -gt 0 ] && drops=$((total - prev_drops))
    prev_drops=$total
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
    if [ "$prev_ot" -gt 0 ] && [ -n "$oticks" ]; then
      odt=$((now-prev_ot)); [ "$odt" -gt 0 ] && ocpu=$(( (oticks-prev_oticks) / odt ))
    fi
    prev_oticks=${oticks:-0}; prev_ot=$now
  fi
  bt=$(settings get global bluetooth_on 2>/dev/null)
  head="acc=$acc phone=$phone load=$load soc=$soc zlink_cpu=$zcpu rx_kbit=$kbit ap_drops=$drops obd_cpu=$ocpu bt=${bt:-na} sta=$sta ap=${ap:-na}"

  if [ "$phone" -gt 0 ]; then
    idle_n=0
    layers=$(dumpsys SurfaceFlinger --list 2>/dev/null | grep 'SurfaceView\[\](BLAST)')
    if [ -z "$layers" ]; then
      emit "$head | no video surface"
    else
      idx=0
      echo "$layers" | while read -r L; do
        idx=$((idx+1))
        dumpsys SurfaceFlinger --latency "$L" </dev/null 2>/dev/null | awk -v layer="${L##*#}" -v idx="$idx" '
          NR==1 { period=$1/1e6; next }
          NF>=3 && $2>0 && $2<9e18 { p[n++]=$2 }
          END {
            if (n<3) { printf "layer=#%s idx=%s frames=%d (idle)\n", layer, idx, n; exit }
            # sort presented timestamps, then the intervals between them
            for (i=0;i<n;i++) for (j=i+1;j<n;j++) if (p[j]<p[i]) { t=p[i]; p[i]=p[j]; p[j]=t }
            m=0; for (i=1;i<n;i++) { d[m++]=(p[i]-p[i-1])/1e6 }
            for (i=0;i<m;i++) for (j=i+1;j<m;j++) if (d[j]<d[i]) { t=d[i]; d[i]=d[j]; d[j]=t }
            med=d[int(m/2)]
            span=(p[n-1]-p[0])/1e9; late=0
            # A late frame is one that missed its slot, and the slot is this surface`s own
            # cadence -- not the display`s. The old threshold was 2.5 display periods, which
            # on a 57 Hz panel is 44 ms; a 30 fps source cannot be shown evenly there and has
            # to alternate 2-vsync (35 ms) and 3-vsync (53 ms) holds, so every ordinary
            # 3-vsync hold counted late. Worse, a surface running steadily at 19 fps scored
            # 100% late while dropping nothing at all. Measuring from the median instead
            # separates the two questions the fields already answer separately: fps says how
            # fast the surface runs, late says how unevenly.
            thr=med+1.5*period
            for (i=0;i<m;i++) if (d[i]>thr) late++
            printf "layer=#%s idx=%s fps=%.1f med=%.1f p95=%.1f max=%.1f late=%d%% n=%d period=%.1f thr=%.1f\n",
              layer, idx, (span>0? n/span:0), med, d[int(m*0.95)-1<0?0:int(m*0.95)-1], d[m-1], 100*late/m, n, period, thr
          }' | while read -r stat; do emit "$head | $stat"; done
      done
    fi
  else
    idle_n=$((idle_n+1))
    # A heartbeat once a minute while no phone is attached: enough to prove it is alive.
    [ $((idle_n % 4)) -eq 1 ] && emit "$head | no phone on hotspot"
  fi
  sleep "$INTERVAL"
done

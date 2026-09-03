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

prev_ticks=0; prev_t=0; prev_rx=0; idle_n=0
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
  head="acc=$acc phone=$phone load=$load soc=$soc zlink_cpu=$zcpu rx_kbit=$kbit sta=$sta ap=${ap:-na}"

  if [ "$phone" -gt 0 ]; then
    idle_n=0
    layers=$(dumpsys SurfaceFlinger --list 2>/dev/null | grep 'SurfaceView\[\](BLAST)')
    if [ -z "$layers" ]; then
      emit "$head | no video surface"
    else
      echo "$layers" | while read -r L; do
        dumpsys SurfaceFlinger --latency "$L" </dev/null 2>/dev/null | awk -v layer="${L##*#}" '
          NR==1 { period=$1/1e6; next }
          NF>=3 && $2>0 && $2<9e18 { p[n++]=$2 }
          END {
            if (n<3) { printf "layer=#%s frames=%d (idle)\n", layer, n; exit }
            # sort presented timestamps, then the intervals between them
            for (i=0;i<n;i++) for (j=i+1;j<n;j++) if (p[j]<p[i]) { t=p[i]; p[i]=p[j]; p[j]=t }
            m=0; for (i=1;i<n;i++) { d[m++]=(p[i]-p[i-1])/1e6 }
            for (i=0;i<m;i++) for (j=i+1;j<m;j++) if (d[j]<d[i]) { t=d[i]; d[i]=d[j]; d[j]=t }
            span=(p[n-1]-p[0])/1e9; late=0; thr=2.5*period
            for (i=0;i<m;i++) if (d[i]>thr) late++
            printf "layer=#%s fps=%.1f med=%.1f p95=%.1f max=%.1f late=%d%% n=%d period=%.1f\n",
              layer, (span>0? n/span:0), d[int(m/2)], d[int(m*0.95)-1<0?0:int(m*0.95)-1], d[m-1], 100*late/m, n, period
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

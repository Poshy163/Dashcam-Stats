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
SELFSTART=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$$/stat" 2>/dev/null)
TOKEN="$$:${SELFSTART:-unknown}:$SESSION:$SEQUENCE"
STATUS=unavailable; ATRACERC=na; OWNED=0; STARTED=0
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
  read owner < "$LOCK/owner" 2>/dev/null
  [ "$owner" = "$TOKEN" ]
}
finish() {
  trap - EXIT TERM INT
  # Reap every owned child before releasing the lock. No delayed atrace process
  # or watchdog may later stop a subsequent capture after a sampler restart.
  if [ -n "$WATCHPID" ]; then kill "$WATCHPID" 2>/dev/null; wait "$WATCHPID" 2>/dev/null; fi
  if [ -n "$ATRACEPID" ]; then
    kill "$ATRACEPID" 2>/dev/null
    kill -KILL "$ATRACEPID" 2>/dev/null
    wait "$ATRACEPID" 2>/dev/null
  fi
  if [ -n "$AWKPID" ]; then kill "$AWKPID" 2>/dev/null; wait "$AWKPID" 2>/dev/null; fi
  read_state
  if [ "$STARTED" = 1 ] && owns_lock && [ "$STATE" = 1 ] && [ "$TRACER" = nop ]; then
    # Only this live invocation may clean up its interrupted capture. Skipped
    # captures and stale-lock recovery never issue an atrace stop command.
    timeout 1 atrace --async_stop >/dev/null 2>&1
    read_state
  fi
  OFF=na
  [ "$STATE" = 0 ] && OFF=1
  [ "$STATE" = 1 ] && OFF=0
  if [ "$STARTED" = 1 ] && [ "$OFF" != 1 ]; then STATUS=restore_failed; fi
  ENDMS=$(clock_ms); ENDMS=${ENDMS:-$STARTMS}
  ELAPSED=$((ENDMS-STARTMS))
  [ "$ELAPSED" -ge 0 ] || ELAPSED=0
  if owns_lock && [ -r "$LOCK/summary" ]; then read SUMMARY < "$LOCK/summary"; fi
  # The parser's output is a fixed whitelist of numeric fields. Error captures
  # retain counts/health flags, but cannot expose partially healthy latency.
  if [ "$STATUS" != ok ]; then
    SUMMARY=$(printf '%s\n' "$SUMMARY" | sed 's/codec_latency_med_ms=[^ ]*/codec_latency_med_ms=na/;s/codec_latency_p95_ms=[^ ]*/codec_latency_p95_ms=na/;s/codec_latency_max_ms=[^ ]*/codec_latency_max_ms=na/')
  fi
  MESSAGE="sample=$SESSION-codec-$SEQUENCE session=$SESSION schema=6 | event=codec_capture codec_capture_status=$STATUS codec_capture_start_ms=$STARTMS codec_capture_end_ms=$ENDMS codec_capture_ms=$ELAPSED codec_trace_off=$OFF codec_atrace_rc=$ATRACERC $SUMMARY"
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$MESSAGE" >> "$LOGFILE"
  log -p "$PRIO" -t CarPlayTiming "$MESSAGE" 2>/dev/null
  if owns_lock; then
    rm -f "$LOCK/pipe" "$LOCK/summary" "$LOCK/owner"
    rmdir "$LOCK" 2>/dev/null
  fi
}
trap finish EXIT
trap 'STATUS=interrupted; exit 0' TERM INT

for path in /sys/kernel/tracing /sys/kernel/debug/tracing; do
  if [ -r "$path/tracing_on" ]; then TRACEPATH=$path; break; fi
done
read_state
if [ "$STATE" != 0 ] || [ "$TRACER" != nop ]; then STATUS=trace_busy_or_unavailable; exit 0; fi
for tool in atrace awk timeout mkfifo; do
  command -v "$tool" >/dev/null 2>&1 || { STATUS=tool_unavailable; exit 0; }
done
set -- $(pidof com.zjinnova.zlink 2>/dev/null)
[ "$#" = 1 ] || { STATUS=zlink_missing_or_ambiguous; exit 0; }
ZPID=$1
case "$ZPID" in ''|*[!0-9]*) STATUS=zlink_missing_or_ambiguous; exit 0;; esac
ZSTART=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$ZPID/stat" 2>/dev/null)
case "$ZSTART" in ''|*[!0-9]*) STATUS=zlink_identity_unavailable; exit 0;; esac
if ! mkdir "$LOCK" 2>/dev/null; then
  # A reboot/forced kill can leave this fixed directory. Recover only a recorded
  # owner proven dead (or its PID demonstrably reused), while tracing is off.
  read previous_owner < "$LOCK/owner" 2>/dev/null
  oldpid=${previous_owner%%:*}; rest=${previous_owner#*:}; oldstart=${rest%%:*}
  dead=0
  case "$oldpid:$oldstart" in ''|*[!0-9:]*) ;;
    *)
      if [ ! -d "/proc/$oldpid" ]; then dead=1
      else
        actual_start=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$oldpid/stat" 2>/dev/null)
        case "$actual_start" in ''|*[!0-9]*) ;; *) [ "$actual_start" = "$oldstart" ] || dead=1;; esac
      fi;;
  esac
  read_state
  read current_owner < "$LOCK/owner" 2>/dev/null
  if [ "$dead" = 1 ] && [ "$STATE" = 0 ] && [ "$TRACER" = nop ] && [ "$current_owner" = "$previous_owner" ]; then
    rm -f "$LOCK/pipe" "$LOCK/summary" "$LOCK/owner"
    rmdir "$LOCK" 2>/dev/null
  fi
  mkdir "$LOCK" 2>/dev/null || { STATUS=capture_busy; exit 0; }
fi
OWNED=1
printf '%s\n' "$TOKEN" > "$LOCK/owner"
mkfifo "$LOCK/pipe" || { STATUS=pipe_failed; exit 0; }
# Recheck after acquiring our guard; never start over an already active tracer.
read_state
if [ "$STATE" != 0 ] || [ "$TRACER" != nop ]; then STATUS=trace_busy_or_unavailable; exit 0; fi

awk -v zpid="$ZPID" '
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
' < "$LOCK/pipe" > "$LOCK/summary" 2>/dev/null &
AWKPID=$!
STARTMS=$(clock_ms); STARTMS=${STARTMS:-0}
STARTED=1
atrace -t 5 -b 2048 gfx video > "$LOCK/pipe" 2>/dev/null &
ATRACEPID=$!
ATRACESTART=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$ATRACEPID/stat" 2>/dev/null)
AWKSTART=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$AWKPID/stat" 2>/dev/null)
(
  sleep_pid=
  trap '[ -z "$sleep_pid" ] || { kill "$sleep_pid" 2>/dev/null; wait "$sleep_pid" 2>/dev/null; }; exit 0' TERM INT
  sleep 8 &
  sleep_pid=$!
  wait "$sleep_pid" || exit 0
  sleep_pid=
  # ATRACERC is collected before the parent clears this watchdog. The parent
  # reaps this watchdog before releasing the lock or starting another capture.
  for owned_child in "$ATRACEPID:$ATRACESTART" "$AWKPID:$AWKSTART"; do
    child_pid=${owned_child%%:*}; child_start=${owned_child#*:}
    [ -n "$child_pid" ] && [ -n "$child_start" ] || continue
    actual_start=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$child_pid/stat" 2>/dev/null)
    # A normally exited producer can have been reaped while awk drains its pipe.
    # Never signal a recycled PID at the overall eight-second deadline.
    [ "$actual_start" = "$child_start" ] && kill -KILL "$child_pid" 2>/dev/null
  done
) &
WATCHPID=$!
wait "$ATRACEPID"; ATRACERC=$?; ATRACEPID=
wait "$AWKPID"; AWKRC=$?; AWKPID=
kill "$WATCHPID" 2>/dev/null; wait "$WATCHPID" 2>/dev/null; WATCHPID=
STATUS=ok
[ "$ATRACERC" = 0 ] || STATUS=atrace_error
[ "$AWKRC" = 0 ] || STATUS=parser_error
ZEND=$(awk '{sub(/^.*\) /,"");print $20}' "/proc/$ZPID/stat" 2>/dev/null)
[ "$ZEND" = "$ZSTART" ] || STATUS=process_changed
if [ -r "$LOCK/summary" ]; then
  read SUMMARY < "$LOCK/summary"
  case "$SUMMARY" in
    codec_stats_status=ok\ *) ;;
    codec_stats_status=no_markers\ *) [ "$STATUS" != ok ] || STATUS=no_markers;;
    *) [ "$STATUS" != ok ] || STATUS=invalid_trace;;
  esac
else
  STATUS=summary_missing
fi
exit 0

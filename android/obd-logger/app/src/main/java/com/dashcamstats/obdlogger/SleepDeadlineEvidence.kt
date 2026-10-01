package com.dashcamstats.obdlogger

import android.content.Context
import android.provider.Settings
import java.io.File
import java.util.Locale

internal const val DEADLINE_OBSERVATION_MAX_AGE_MS = 15_000L
internal const val DEADLINE_CAPTURE_MAX_MS = 6_000L
internal const val DEADLINE_STATUS_HEARTBEAT_MS = 10_000L

internal data class SleepBootIdentity(val bootId: String?, val bootCount: Int?) {
    val valid: Boolean get() = bootId != null || bootCount != null
}

/** Monotonic evidence, not a claim that writing a property restarted the native timer. */
data class SleepDeadlineEvidence(
    val bootId: String?,
    val bootCount: Int?,
    val observedElapsedMillis: Long,
    val ignitionOn: Boolean,
    val offLowerElapsedMillis: Long? = null,
    val offUpperElapsedMillis: Long? = null,
    val windowSeconds: Int? = null,
)

internal fun readSleepBootIdentity(context: Context): SleepBootIdentity {
    val uuid = runCatching {
        File("/proc/sys/kernel/random/boot_id").reader().use { reader ->
            val buffer = CharArray(64)
            val count = reader.read(buffer)
            if (count < 0) null else String(buffer, 0, count).trim().lowercase(Locale.ROOT)
                .takeIf { it.matches(Regex("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")) }
        }
    }.getOrNull()
    val count = runCatching {
        Settings.Global.getInt(context.contentResolver, Settings.Global.BOOT_COUNT, -1)
            .takeIf { it >= 0 }
    }.getOrNull()
    return SleepBootIdentity(uuid, count)
}

/** An OFF sample alone is never an edge. Invalid continuity requires a new ON sample. */
internal class SleepDeadlineTracker {
    private data class Sample(
        val boot: SleepBootIdentity,
        val started: Long,
        val finished: Long,
        val on: Boolean,
        val window: Int,
    )

    private var previous: Sample? = null
    private var evidence: SleepDeadlineEvidence? = null

    @Synchronized
    fun observe(
        boot: SleepBootIdentity,
        startedElapsedMillis: Long,
        finishedElapsedMillis: Long,
        ignitionOn: Boolean?,
        windowSeconds: Int?,
    ) {
        if (
            !boot.valid || startedElapsedMillis < 0 || finishedElapsedMillis < startedElapsedMillis ||
            finishedElapsedMillis - startedElapsedMillis > DEADLINE_CAPTURE_MAX_MS ||
            ignitionOn == null || windowSeconds == null || windowSeconds !in 1..3_600
        ) {
            invalidate()
            return
        }
        val old = previous?.takeIf {
            it.boot == boot && startedElapsedMillis >= it.finished &&
                finishedElapsedMillis - it.finished <= DEADLINE_OBSERVATION_MAX_AGE_MS
        }
        if (old == null) evidence = null
        val edge = when {
            ignitionOn -> null
            old?.on == true -> SleepDeadlineEvidence(
                boot.bootId, boot.bootCount, finishedElapsedMillis, false,
                old.started, finishedElapsedMillis, minOf(old.window, windowSeconds),
            )
            else -> evidence
        }
        evidence = SleepDeadlineEvidence(
            boot.bootId, boot.bootCount, finishedElapsedMillis, ignitionOn,
            edge?.offLowerElapsedMillis, edge?.offUpperElapsedMillis, edge?.windowSeconds,
        )
        previous = Sample(boot, startedElapsedMillis, finishedElapsedMillis, ignitionOn, windowSeconds)
    }

    @Synchronized
    fun snapshot(nowElapsedMillis: Long): SleepDeadlineEvidence? {
        val last = previous ?: return null
        if (
            nowElapsedMillis < last.finished ||
            nowElapsedMillis - last.finished > DEADLINE_OBSERVATION_MAX_AGE_MS
        ) {
            invalidate()
        }
        return evidence
    }

    private fun invalidate() {
        previous = null
        evidence = null
    }
}

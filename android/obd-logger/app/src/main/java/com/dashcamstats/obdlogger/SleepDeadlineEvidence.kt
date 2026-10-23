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

/**
 * An OFF sample alone is never an edge. App-private checkpoints preserve the same bounded
 * continuity across an ordinary process restart/update; they cannot widen it or start a timer.
 */
internal class SleepDeadlineTracker(private val store: SleepDeadlineCheckpointStore? = null) {
    private data class Sample(
        val boot: SleepBootIdentity,
        val started: Long,
        val finished: Long,
        val on: Boolean,
        val window: Int,
    )

    private var previous: Sample? = null
    private var evidence: SleepDeadlineEvidence? = null
    private var loadCheckpoint = store != null
    private val storeLock = Any()
    private var generation = 0L
    private var persistenceRequested = false
    private var lastQueuedCheckpoint: SleepDeadlineCheckpoint? = null
    private var pendingSnapshotInvalidation: Persistence? = null

    private data class Persistence(val generation: Long, val checkpoint: SleepDeadlineCheckpoint?)

    fun observe(
        boot: SleepBootIdentity,
        startedElapsedMillis: Long,
        finishedElapsedMillis: Long,
        ignitionOn: Boolean?,
        windowSeconds: Int?,
    ) {
        flushPendingInvalidation()
        val shouldLoad = synchronized(this) {
            if (loadCheckpoint) { loadCheckpoint = false; true } else false
        }
        val checkpoint = if (shouldLoad) synchronized(storeLock) {
            runCatching { store?.read() }.getOrNull()
        } else null
        val update = synchronized(this) {
            observeLocked(boot, startedElapsedMillis, finishedElapsedMillis, ignitionOn, windowSeconds,
                shouldLoad, checkpoint)
        }
        persist(update)
    }

    private fun observeLocked(
        boot: SleepBootIdentity,
        startedElapsedMillis: Long,
        finishedElapsedMillis: Long,
        ignitionOn: Boolean?,
        windowSeconds: Int?,
        shouldLoad: Boolean,
        checkpoint: SleepDeadlineCheckpoint?,
    ): Persistence? {
        if (
            !boot.valid || startedElapsedMillis < 0 || finishedElapsedMillis < startedElapsedMillis ||
            finishedElapsedMillis - startedElapsedMillis > DEADLINE_CAPTURE_MAX_MS ||
            ignitionOn == null || windowSeconds == null || windowSeconds !in 1..3_600
        ) {
            return invalidate()
        }
        var invalidation: Persistence? = null
        if (shouldLoad) {
            if (checkpoint != null && checkpoint.valid && checkpoint.boot == boot &&
                startedElapsedMillis >= checkpoint.finishedElapsedMillis &&
                finishedElapsedMillis - checkpoint.finishedElapsedMillis <= DEADLINE_OBSERVATION_MAX_AGE_MS &&
                windowSeconds == checkpoint.observedWindowSeconds &&
                (checkpoint.offLowerElapsedMillis == null || finishedElapsedMillis <
                    checkpoint.offLowerElapsedMillis + checkNotNull(checkpoint.windowSeconds) * 1_000L)
            ) {
                previous = Sample(
                    checkpoint.boot, checkpoint.startedElapsedMillis, checkpoint.finishedElapsedMillis,
                    checkpoint.ignitionOn, checkpoint.observedWindowSeconds,
                )
                evidence = checkpoint.evidence
            } else {
                // A read failure, reboot, changed property or missed observation is not permission
                // to infer an edge. Only a newly observed ON can seed subsequent timing.
                invalidation = invalidate()
            }
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
        }?.takeUnless {
            val lower = it.offLowerElapsedMillis
            val seconds = it.windowSeconds
            lower != null && seconds != null && finishedElapsedMillis >= lower + seconds * 1_000L
        }
        evidence = SleepDeadlineEvidence(
            boot.bootId, boot.bootCount, finishedElapsedMillis, ignitionOn,
            edge?.offLowerElapsedMillis, edge?.offUpperElapsedMillis, edge?.windowSeconds,
        )
        previous = Sample(boot, startedElapsedMillis, finishedElapsedMillis, ignitionOn, windowSeconds)
        val current = checkNotNull(evidence)
        return queuePersistence(if (boot.bootId != null && current.offLowerElapsedMillis != null) {
            SleepDeadlineCheckpoint(
                boot, startedElapsedMillis, finishedElapsedMillis, ignitionOn, windowSeconds,
                current.offLowerElapsedMillis, current.offUpperElapsedMillis, current.windowSeconds,
            )
        } else null) ?: invalidation
    }

    fun snapshot(nowElapsedMillis: Long): SleepDeadlineEvidence? {
        return synchronized(this) {
            val last = previous ?: return null
            if (nowElapsedMillis < last.finished ||
                nowElapsedMillis - last.finished > DEADLINE_OBSERVATION_MAX_AGE_MS
            ) {
                // A status/BLE caller never waits for storage. An expired/future checkpoint is
                // already inadmissible to a new process; the IO worker commits its revocation.
                pendingSnapshotInvalidation = invalidate()
            }
            evidence
        }
    }

    fun flushPendingInvalidation() {
        val invalidation = synchronized(this) {
            pendingSnapshotInvalidation.also { pendingSnapshotInvalidation = null }
        }
        persist(invalidation)
    }

    private fun invalidate(): Persistence? {
        loadCheckpoint = false
        previous = null
        evidence = null
        return queuePersistence(null)
    }

    private fun queuePersistence(checkpoint: SleepDeadlineCheckpoint?): Persistence? {
        if (store == null || (persistenceRequested && checkpoint == lastQueuedCheckpoint)) return null
        persistenceRequested = true
        lastQueuedCheckpoint = checkpoint
        return Persistence(++generation, checkpoint)
    }

    private fun persist(update: Persistence?) {
        if (update == null) return
        synchronized(storeLock) {
            if (synchronized(this) { generation != update.generation }) return
            // Disk transactions run on the controller's existing IO worker, outside the tracker
            // state monitor: a routine BLE/status snapshot cannot queue behind an fsync.
            if (runCatching { store?.write(update.checkpoint) }.isFailure) {
                synchronized(this) {
                    if (generation == update.generation) invalidate()
                }
                runCatching { store?.write(null) }
            }
        }
    }
}

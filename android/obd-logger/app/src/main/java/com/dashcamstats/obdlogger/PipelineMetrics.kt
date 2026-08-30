package com.dashcamstats.obdlogger

import org.json.JSONObject

/**
 * Fixed-schema, saturating counters for the direct synchronous OBD pipeline.
 *
 * The logger deliberately exposes no commands, response bodies, PID values, adapter identity or
 * exception messages here. Counter values saturate rather than overflowing and the synchronous
 * persistence path reports its real bounded queue depth of zero or one.
 */
class PipelineMetrics(private val maximumCounter: Long = Int.MAX_VALUE.toLong()) {
    init {
        require(maximumCounter >= 1)
    }

    private val counters = linkedMapOf(
        "commands_requested" to 0L,
        "commands_completed" to 0L,
        "command_timeouts" to 0L,
        "notifications_received" to 0L,
        "notification_fragments_received" to 0L,
        "frames_assembled" to 0L,
        "checksum_failures" to 0L,
        "parse_failures" to 0L,
        "samples_created" to 0L,
        "samples_queued" to 0L,
        "samples_persisted" to 0L,
        "samples_dropped" to 0L,
        "database_write_failures" to 0L,
        "ble_disconnects" to 0L,
        "reconnect_attempts" to 0L,
        "radio_shutdowns" to 0L,
    )
    private var queueDepth = 0L
    private var maximumQueueDepth = 0L

    @Synchronized
    private fun increment(name: String) {
        counters[name] = ((counters[name] ?: error("unknown metric $name")) + 1)
            .coerceAtMost(maximumCounter)
    }

    fun commandRequested() = increment("commands_requested")
    fun commandCompleted() = increment("commands_completed")
    fun commandTimedOut() = increment("command_timeouts")

    fun notificationReceived() {
        increment("notifications_received")
        increment("notification_fragments_received")
    }

    fun frameAssembled() = increment("frames_assembled")

    fun parserFailure(checksumFailure: Boolean) {
        increment("parse_failures")
        if (checksumFailure) increment("checksum_failures")
    }

    fun sampleCreated() = increment("samples_created")

    @Synchronized
    fun sampleQueued() {
        increment("samples_queued")
        queueDepth = (queueDepth + 1).coerceAtMost(1)
        maximumQueueDepth = maxOf(maximumQueueDepth, queueDepth)
    }

    @Synchronized
    fun samplePersisted() {
        increment("samples_persisted")
        queueDepth = (queueDepth - 1).coerceAtLeast(0)
    }

    @Synchronized
    fun sampleDropped() {
        increment("samples_dropped")
        queueDepth = (queueDepth - 1).coerceAtLeast(0)
    }

    @Synchronized
    fun databaseWriteFailed() {
        increment("database_write_failures")
        queueDepth = (queueDepth - 1).coerceAtLeast(0)
    }

    fun bleDisconnected() = increment("ble_disconnects")
    fun reconnectAttempted() = increment("reconnect_attempts")
    fun radioShutdownObserved() = increment("radio_shutdowns")

    @Synchronized
    fun snapshot(): PipelineMetricsSnapshot = PipelineMetricsSnapshot(
        values = LinkedHashMap(counters),
        queueDepth = queueDepth,
        maximumQueueDepth = maximumQueueDepth,
    )
}

data class PipelineMetricsSnapshot(
    val values: Map<String, Long>,
    val queueDepth: Long,
    val maximumQueueDepth: Long,
) {
    fun toJson(): JSONObject = JSONObject().also { body ->
        for ((key, value) in values) body.put(key, value)
        body.put("queue_depth", queueDepth)
        body.put("maximum_queue_depth", maximumQueueDepth)
    }

    companion object {
        val EMPTY = PipelineMetrics().snapshot()
    }
}

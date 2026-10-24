package com.dashcamstats.obdlogger

import org.json.JSONArray
import org.json.JSONObject

/** Bounded opportunities within a caller-supplied fixed horizon, never a new deadline.
 * Both per-identity attempts and the shared cooling window survive a process restart.
 * A shared window prevents cycling through a full card's hot clips every indexer tick.
 */
internal class QuietResourceRetry {
    private data class Attempt(val count: Int, val afterMs: Long)
    private val attempts = linkedMapOf<String, Attempt>()
    private var observedAtMs = 0L
    private var cooldownUntilMs = 0L
    private var refusalStreak = 0

    fun advance(now: Long): Boolean {
        val rollback = now < observedAtMs
        val invalidated = rollback && (cooldownUntilMs != 0L || attempts.values.any {
            it.count < MAX_ATTEMPTS || it.afterMs != 0L
        })
        if (rollback) {
            // A wall-clock rollback cannot reset attempt budgets or prolong a cooling hold.
            attempts.replaceAll { _, value -> value.copy(count = MAX_ATTEMPTS, afterMs = 0) }
            cooldownUntilMs = 0
        }
        observedAtMs = maxOf(observedAtMs, now)
        return invalidated
    }

    fun retain(keys: Set<String>) { attempts.keys.retainAll(keys) }

    fun pending(key: String, visual: JSONObject, now: Long, deadline: Long): Boolean {
        val attempt = attempts[key] ?: return false
        return transient(visual) && attempt.count < MAX_ATTEMPTS && now < deadline && now >= observedAtMs
    }

    fun ready(key: String, visual: JSONObject?, now: Long, deadline: Long): Boolean =
        now >= cooldownUntilMs && now >= observedAtMs && now < deadline &&
            (visual == null || (pending(key, visual, now, deadline) && now >= checkNotNull(attempts[key]).afterMs))

    fun record(key: String, visual: JSONObject, now: Long) {
        advance(now)
        if (transient(visual)) {
            refusalStreak = (refusalStreak + 1).coerceAtMost(3)
            val delayMs = 30_000L shl (refusalStreak - 1)
            val after = now.coerceAtMost(Long.MAX_VALUE - delayMs) + delayMs
            // Hot admission probes return before opening video and have no frame progress.
            // Preserve a later opportunity; queued input or decoded output spends an attempt.
            val started = visual.optInt("input_frames", -1) > 0 || visual.optInt("decoded_frames", -1) > 0
            val count = ((attempts[key]?.count ?: 0) + if (started) 1 else 0).coerceAtMost(MAX_ATTEMPTS)
            attempts.remove(key)
            attempts[key] = Attempt(count, after)
            cooldownUntilMs = after
            while (attempts.size > MAX_ENTRIES) attempts.remove(attempts.keys.first())
        } else {
            attempts.remove(key)
            refusalStreak = 0
            cooldownUntilMs = 0
        }
    }

    fun json(): JSONObject = JSONObject().put("observed_at_ms", observedAtMs)
        .put("cooldown_until_ms", cooldownUntilMs).put("refusal_streak", refusalStreak)
        .put("attempts", JSONArray(attempts.map { (key, value) -> JSONObject().put("key", key)
            .put("count", value.count).put("after_ms", value.afterMs) }))

    companion object {
        const val MAX_ATTEMPTS = 4
        private const val MAX_ENTRIES = 128
        private val reasons = setOf(QuietAnalysisReason.CPU_TEMPERATURE_GUARD.wire,
            QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT.wire, QuietAnalysisReason.SYSTEM_THERMAL_GUARD.wire,
            QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD.wire, QuietAnalysisReason.MEMORY_GUARD.wire)

        fun transient(visual: JSONObject): Boolean = visual.optString("status") == "unavailable" &&
            visual.optString("reason") in reasons && visual.optInt("motion_frames", -1) == 0 &&
            visual.optInt("max_coherent_pixels", -1) in 0 until 16 &&
            visual.optInt("max_rolling_coherent_pixels", -1) in 0 until 128 &&
            visual.optInt("max_consecutive_coherent_frames", -1) in 0 until 2

        fun fromJson(value: JSONObject?): QuietResourceRetry = runCatching {
            require(value != null)
            QuietResourceRetry().apply {
                observedAtMs = value.getLong("observed_at_ms").also { require(it in 0..Long.MAX_VALUE - 120_000L) }
                cooldownUntilMs = value.getLong("cooldown_until_ms").also {
                    require(it >= 0 && it <= observedAtMs + 120_000L)
                }
                refusalStreak = value.getInt("refusal_streak").also { require(it in 0..3) }
                val rows = value.getJSONArray("attempts")
                require(rows.length() <= MAX_ENTRIES)
                for (i in 0 until rows.length()) {
                    val row = rows.getJSONObject(i)
                    val key = row.getString("key").also { require(it.matches(Regex("[a-f0-9]{64}"))) }
                    val count = row.getInt("count").also { require(it in 0..MAX_ATTEMPTS) }
                    val after = row.getLong("after_ms").also { require(it >= 0 && it <= observedAtMs + 120_000L) }
                    require(attempts.put(key, Attempt(count, after)) == null)
                }
            }
        }.getOrElse { QuietResourceRetry() }
    }
}

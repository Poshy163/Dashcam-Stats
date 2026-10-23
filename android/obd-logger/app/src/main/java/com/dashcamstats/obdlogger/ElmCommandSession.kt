package com.dashcamstats.obdlogger

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeout
import java.io.ByteArrayOutputStream
import java.time.Instant

/** Connection-scoped clock whose evidence deliberately survives close/cancellation finalisation. */
internal class SuccessfulResponseClock {
    @Volatile
    var lastSuccessfulResponseAtUtc: String? = null
        private set

    fun freshConnection() {
        lastSuccessfulResponseAtUtc = null
    }

    fun responseCompleted(atUtc: String) {
        lastSuccessfulResponseAtUtc = Instant.parse(atUtc).toString()
    }

    fun beginDriveEvidence() {
        lastSuccessfulResponseAtUtc = null
    }

    /** Disconnect does not erase evidence; the next genuinely fresh connection does. */
    fun disconnected() = Unit
}

internal sealed interface ElmResponseAssembly {
    data object Pending : ElmResponseAssembly
    data object Ignored : ElmResponseAssembly
    data object Overflow : ElmResponseAssembly
    data object TrailingData : ElmResponseAssembly
    data object UnexpectedData : ElmResponseAssembly
    data class Complete(val response: String) : ElmResponseAssembly
}

/** Prompt-delimited assembler shared by the real BLE callback and JVM tests. */
internal class ElmResponseAssembler(private val maximumBytes: Int = 8192) {
    private val buffer = ByteArrayOutputStream()

    init {
        require(maximumBytes > 0)
    }

    fun reset() = buffer.reset()

    fun accept(fragment: ByteArray): ElmResponseAssembly {
        if (buffer.size() + fragment.size > maximumBytes) return ElmResponseAssembly.Overflow
        buffer.write(fragment)
        val accumulated = buffer.toByteArray()
        val prompt = accumulated.indexOf('>'.code.toByte())
        if (prompt < 0) return ElmResponseAssembly.Pending
        // Never carry bytes after one prompt into the next command. Whitespace/NUL padding is
        // harmless, but another prompt or any other byte makes response ownership ambiguous.
        val unsafeTrailing = accumulated.copyOfRange(prompt + 1, accumulated.size).any { byte ->
            byte != 0.toByte() && !byte.toInt().toChar().isWhitespace()
        }
        if (unsafeTrailing) {
            buffer.reset()
            return ElmResponseAssembly.TrailingData
        }
        val response = accumulated.copyOfRange(0, prompt + 1)
            .toString(Charsets.US_ASCII)
            .replace("\u0000", "")
        buffer.reset()
        return ElmResponseAssembly.Complete(response)
    }
}

/**
 * Owns command response state. An uncertain timeout, malformed stream or uncertain write remains
 * tainted across disconnect. Only an explicitly bounded optional-command drain can avoid that
 * taint, and only after its original prompt and a separate adapter confirmation are proven.
 */
internal class ElmCommandSession(maximumResponseBytes: Int = 8192) {
    private val assembler = ElmResponseAssembler(maximumResponseBytes)
    private var waitingForPrompt = false

    @Volatile
    var isTainted: Boolean = false
        private set

    @Synchronized
    fun freshConnection() {
        assembler.reset()
        waitingForPrompt = false
        isTainted = false
    }

    @Synchronized
    fun beginCommand() {
        if (isTainted) throw ElmException("tainted ELM session requires disconnect and fresh ATZ")
        check(!waitingForPrompt) { "an ELM command is already awaiting its prompt" }
        assembler.reset()
        waitingForPrompt = true
    }

    @Synchronized
    fun accept(fragment: ByteArray): ElmResponseAssembly {
        if (!waitingForPrompt) {
            val carriesData = fragment.any { byte ->
                byte != 0.toByte() && !byte.toInt().toChar().isWhitespace()
            }
            if (carriesData) {
                isTainted = true
                return ElmResponseAssembly.UnexpectedData
            }
            return ElmResponseAssembly.Ignored
        }
        return when (val assembled = assembler.accept(fragment)) {
            ElmResponseAssembly.Overflow,
            ElmResponseAssembly.TrailingData,
            -> {
                waitingForPrompt = false
                isTainted = true
                assembled
            }
            is ElmResponseAssembly.Complete -> {
                waitingForPrompt = false
                assembled
            }
            else -> assembled
        }
    }

    @Synchronized
    fun taint() {
        waitingForPrompt = false
        isTainted = true
    }

    @Synchronized
    fun disconnected() {
        assembler.reset()
        waitingForPrompt = false
        // Deliberately preserve isTainted. A fresh GATT connection is the only reset boundary.
    }

    @Synchronized
    private fun requirePromptBoundary() {
        if (isTainted || waitingForPrompt) throw ElmException("optional command recovery lost its prompt boundary")
    }

    suspend fun awaitResponse(
        pending: CompletableDeferred<String>,
        command: String,
        timeoutMillis: Long,
        optionalRecovery: OptionalCommandRecovery? = null,
    ): String = try {
        withTimeout(timeoutMillis) { pending.await() }
    } catch (_: TimeoutCancellationException) {
        if (optionalRecovery != null) {
            try {
                withTimeout(OPTIONAL_RECOVERY_LIMIT_MILLIS) {
                    // Retain ownership of the timed-out command. Its eventual reply is
                    // discarded, including an otherwise valid PID value.
                    if (!pending.isCompleted) optionalRecovery.abort()
                    pending.await()
                    requirePromptBoundary()
                    delay(OPTIONAL_RECOVERY_SETTLE_MILLIS)
                    requirePromptBoundary()
                    val confirmation = optionalRecovery.confirm()
                    if (!isOptionalRecoveryConfirmation(confirmation)) {
                        throw ElmException("optional command recovery confirmation was invalid")
                    }
                    delay(OPTIONAL_RECOVERY_SETTLE_MILLIS)
                    requirePromptBoundary()
                }
            } catch (cancelled: CancellationException) {
                taint()
                if (cancelled !is TimeoutCancellationException) throw cancelled
                throw ElmCommandTimeoutException("optional command prompt recovery timed out")
            } catch (error: Exception) {
                taint()
                throw error
            }
            throw ElmCommandTimeoutException(
                "optional command timed out; prompt resynchronised and reading omitted",
                promptRecovered = true,
            )
        }
        taint()
        throw ElmCommandTimeoutException("timed out waiting for prompt from $command")
    } catch (cancelled: CancellationException) {
        taint()
        throw cancelled
    } catch (error: Exception) {
        taint()
        throw error
    }
}

internal const val OPTIONAL_RECOVERY_LIMIT_MILLIS = 2_000L
internal const val OPTIONAL_RECOVERY_SETTLE_MILLIS = 200L
// ELM327 data sheet, Communicating with the ELM327: any received character
// interrupts OBD processing; spaces are ignored as input when idle. Never use CR,
// which repeats the preceding command if its prompt is merely delayed in BLE.
internal const val OPTIONAL_COMMAND_ABORT_CHARACTER = ' '
internal data class OptionalCommandRecovery(
    val abort: suspend () -> Unit,
    val confirm: suspend () -> String,
)

internal fun isOptionalRecoveryConfirmation(response: String): Boolean {
    if (!response.endsWith('>')) return false
    val lines = response.dropLast(1).split('\r', '\n').map(String::trim).filter(String::isNotEmpty)
    return lines == listOf("OK") || lines == listOf("ATE0", "OK")
}

/**
 * Connection-scoped request formatting for live PIDs. A prompt-complete invalid reply is never
 * telemetry. It selects the ordinary request form for that PID's next scheduled observation,
 * without adding an extra bus command to the current cycle or relaxing ISO validation.
 */
internal class ElmLivePidReader(private val suffixDisabled: () -> Unit = {}) {
    @Volatile
    var responseCountSuffixActive: Boolean = true
        private set

    private val plainRequestPids = mutableSetOf<Int>()

    @Synchronized
    fun freshConnection() {
        responseCountSuffixActive = true
        plainRequestPids.clear()
    }

    @Synchronized
    private fun request(pid: Int, plain: String): String =
        if (responseCountSuffixActive && pid !in plainRequestPids) plain + "1" else plain

    @Synchronized
    private fun usePlainRequest(pid: Int): Boolean = plainRequestPids.add(pid)

    @Synchronized
    fun recoveredTimeout(pid: Int) {
        plainRequestPids.add(pid)
    }

    suspend fun queryPayload(
        pid: Int,
        expectedSource: Int,
        execute: suspend (String) -> String,
    ): ByteArray? {
        val length = ElmProtocol.pidLengths[pid] ?: return null
        val plain = "01%02X".format(pid)
        var command = request(pid, plain)
        var reply = execute(command)
        if (command != plain && ElmProtocol.isUnrecognised(reply)) {
            // Preserve the established whole-session fallback for adapters which reject the
            // suffix syntax itself. This is distinct from one PID returning invalid ISO data.
            responseCountSuffixActive = false
            suffixDisabled()
            command = plain
            reply = execute(command)
        }
        if (reply.uppercase().contains("NO DATA")) return null
        if (ElmProtocol.hasTransportError(reply)) throw ElmException("transport error for $command")
        ElmProtocol.negativeResponse(reply, command, 0x01, expectedSource)?.let { nrc ->
            throw ElmCommandRejectedException("ECU rejected $command with NRC %02X".format(nrc))
        }
        try {
            return ElmProtocol.requirePayload(reply, command, pid, length, expectedSource).second
        } catch (error: ElmProtocolException) {
            if (command != plain && usePlainRequest(pid)) {
                // Fixed, bounded metadata only: never include response bytes in diagnostics.
                throw ElmProtocolException(
                    "$command reply failed ISO header/length/source/checksum validation; " +
                        "next scheduled poll uses a plain request",
                )
            }
            throw error
        }
    }
}

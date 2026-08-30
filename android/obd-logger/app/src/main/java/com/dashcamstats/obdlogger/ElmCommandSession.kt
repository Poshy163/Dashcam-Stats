package com.dashcamstats.obdlogger

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.TimeoutCancellationException
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
 * Owns command response state. A timeout, malformed stream or uncertain write remains tainted
 * across disconnect; only completion of a genuinely fresh GATT connection makes commands legal.
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

    suspend fun awaitResponse(
        pending: CompletableDeferred<String>,
        command: String,
        timeoutMillis: Long,
    ): String = try {
        withTimeout(timeoutMillis) { pending.await() }
    } catch (_: TimeoutCancellationException) {
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

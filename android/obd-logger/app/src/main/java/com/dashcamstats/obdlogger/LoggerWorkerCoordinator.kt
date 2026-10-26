package com.dashcamstats.obdlogger

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch

/**
 * Owns the actual logger separately from its pending replacement. Repeated configuration applies
 * coalesce while the old logger finalises, rather than cancelling the coroutine that is joining it.
 */
internal class LoggerWorkerCoordinator(
    private val scope: CoroutineScope,
    private val afterWorkerStopped: suspend () -> Unit = {},
) {
    private val lock = Any()
    private var worker: Job? = null
    private var replacement: Job? = null
    private var pendingRun: (suspend (Boolean) -> Unit)? = null
    private var closed = false

    fun request(reloadConfiguration: Boolean, run: suspend (restarting: Boolean) -> Unit) {
        synchronized(lock) {
            if (closed) return
            if (replacement?.isActive == true) {
                if (reloadConfiguration) pendingRun = run
                return
            }
            val previous = worker?.takeIf { it.isActive }
            if (previous == null) {
                worker = launchWorker(run, restarting = false)
            } else if (reloadConfiguration) {
                pendingRun = run
                replacement = scope.launch(start = CoroutineStart.LAZY) {
                    previous.cancelAndJoin()
                    afterWorkerStopped()
                    synchronized(lock) {
                        if (!closed) {
                            val next = checkNotNull(pendingRun)
                            pendingRun = null
                            worker = launchWorker(next, restarting = true)
                        }
                        replacement = null
                    }
                }.also { it.start() }
            }
        }
    }

    private fun launchWorker(run: suspend (Boolean) -> Unit, restarting: Boolean): Job =
        scope.launch(start = CoroutineStart.LAZY) { run(restarting) }.also { it.start() }

    fun close() {
        synchronized(lock) {
            closed = true
            pendingRun = null
            replacement?.cancel()
            worker?.cancel()
        }
    }
}

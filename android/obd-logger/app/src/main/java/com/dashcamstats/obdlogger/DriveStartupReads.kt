package com.dashcamstats.obdlogger

/** Prioritise the first motion readings, then fit one-off metadata into normal polling budgets. */
internal class DriveStartupReads(
    supported: Set<Int>,
    private val captureProtocol: suspend () -> Unit,
    private val readStatic: suspend (Int) -> Map<String, Any>,
) {
    private var protocolPending = true
    private val staticPids = ArrayDeque(ObdPollPlan.staticOnce.filter(supported::contains))
    val values: Map<String, Any> get() = observed
    private val observed = mutableMapOf<String, Any>()

    // Alternate with unmodified live cycles until all metadata has been attempted. Due
    // MAF and voltage keep their normal priority even on a reserved cycle.
    fun reserve(sequence: Long): Boolean =
        (protocolPending || staticPids.isNotEmpty()) && sequence % 2L == 0L

    suspend fun readOne() {
        if (protocolPending) {
            protocolPending = false
            captureProtocol()
        } else if (staticPids.isNotEmpty()) {
            observed.putAll(readStatic(staticPids.removeFirst()))
        }
    }

    companion object {
        // The trial already puts speed, RPM and MAF first. Keep its critical trio intact.
        fun firstBaselinePids(requested: List<Int>): List<Int> =
            listOf(0x0D, 0x0C).filter(requested::contains)

        fun firstTrialAuxiliary(due: List<Int>, criticalMeasured: Boolean): List<Int> =
            if (criticalMeasured) emptyList() else due.filter { it == PollTrialScheduler.VOLTAGE }
    }
}

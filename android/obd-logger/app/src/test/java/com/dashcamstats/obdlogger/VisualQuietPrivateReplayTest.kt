package com.dashcamstats.obdlogger

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/** Optional local acceptance hook. Private footage-derived grids stay outside the repository.
 * Set GRID_REPLAY_DIR to an ignored directory of .grid/.pts pairs; only numerical proof is emitted. */
@RunWith(RobolectricTestRunner::class)
class VisualQuietPrivateReplayTest {
    @Test fun replayPrivateGridsThroughTheProductionTracker() {
        val configured = System.getenv("GRID_REPLAY_DIR")
        assumeTrue("Private visual calibration fixtures were not provided", !configured.isNullOrBlank())
        val directory = File(checkNotNull(configured))
        val grids = directory.listFiles { file -> file.extension == "grid" }.orEmpty().sortedBy { it.name }
        assertTrue("No private .grid fixtures found", grids.isNotEmpty())
        for (grid in grids) {
            val ptsFile = File(directory, grid.nameWithoutExtension + ".pts")
            val pts = ptsFile.readLines().filter { it.isNotBlank() }.map { it.trim().toLong() }
            val frameBytes = QUIET_GRID_WIDTH * QUIET_GRID_HEIGHT
            assertEquals(pts.size.toLong() * frameBytes, grid.length())
            val tracker = QuietFrameTracker()
            var earlyStatus: String? = null
            var earlyFrame = 0
            grid.inputStream().buffered().use { stream ->
                val bytes = ByteArray(frameBytes)
                for (index in pts.indices) {
                    var offset = 0
                    while (offset < bytes.size) { val count = stream.read(bytes, offset, bytes.size - offset); check(count > 0); offset += count }
                    tracker.accept(IntArray(frameBytes) { bytes[it].toInt() and 255 }, pts[index])
                    if (earlyStatus == null && (tracker.motionFrames > 0 || !tracker.qualityValid)) {
                        earlyStatus = if (!tracker.qualityValid) "unavailable" else "activity"
                        earlyFrame = index + 1
                    }
                }
            }
            val proof = tracker.proof(pts.size, true, true)
            proof.put("first_upload_decision", earlyStatus ?: proof.getString("status"))
                .put("first_upload_frame", earlyFrame)
            File(directory, grid.nameWithoutExtension + ".result.json").writeText(proof.toString(2))
            val expected = File(directory, grid.nameWithoutExtension + ".expected")
            if (expected.isFile) assertEquals(grid.name, expected.readText().trim(), earlyStatus ?: proof.getString("status"))
        }
    }
}

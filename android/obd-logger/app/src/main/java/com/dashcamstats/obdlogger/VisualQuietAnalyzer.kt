package com.dashcamstats.obdlogger

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.ImageFormat
import android.media.Image
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaExtractor
import android.media.MediaFormat
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.os.SystemClock
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.util.ArrayDeque
import kotlin.math.abs
import kotlin.math.max

internal const val QUIET_GRID_WIDTH = 160
internal const val QUIET_GRID_HEIGHT = 90
internal const val QUIET_ALGORITHM_VERSION = 2
internal const val QUIET_ROI_ROWS = 86
internal const val QUIET_ROI_PROFILE = "dashcam_osd_bottom4_v1"
internal const val QUIET_MOTION_METRIC = "adjacent_rolling6_coherent3x3_v1"
// Resource admission/continuation changed; an older refusal must not suppress a new analysis.
internal const val QUIET_RESOURCE_POLICY_VERSION = 3

internal enum class QuietThermalPhase { ADMISSION, DECODING }

/** Fixed public categories only: never publish an exception, codec name, URI or file path. */
internal enum class QuietAnalysisReason(val wire: String) {
    POLICY_NOT_PERMITTED("policy_not_permitted"),
    BATTERY_TEMPERATURE_UNAVAILABLE("battery_temperature_unavailable"),
    BATTERY_TEMPERATURE_GUARD("battery_temperature_guard"),
    SYSTEM_THERMAL_UNAVAILABLE("system_thermal_unavailable"),
    SYSTEM_THERMAL_GUARD("system_thermal_guard"),
    MEMORY_PROBE_UNAVAILABLE("memory_probe_unavailable"),
    MEMORY_GUARD("memory_guard"),
    CPU_TEMPERATURE_UNAVAILABLE("cpu_temperature_unavailable"),
    CPU_TEMPERATURE_GUARD("cpu_temperature_guard"),
    CPU_TEMPERATURE_HARD_LIMIT("cpu_temperature_hard_limit"),
    HARDWARE_DECODER_UNAVAILABLE("hardware_decoder_unavailable"),
    SOURCE_UNAVAILABLE("source_unavailable"),
    FORMAT_UNSUPPORTED("format_unsupported"),
    DECODER_FAILURE("decoder_failure"),
    IMAGE_UNAVAILABLE("image_unavailable"),
    FRAME_QUALITY("frame_quality"),
    VISUAL_ACTIVITY("visual_activity"),
    COVERAGE_INCOMPLETE("coverage_incomplete"),
    ANALYSIS_TIMEOUT("analysis_timeout"),
    FRAME_LIMIT("frame_limit"),
    ANALYSIS_PENDING("analysis_pending"),
    RESOURCE_COOLDOWN("resource_cooldown"),
    CLIP_CHANGED("clip_changed"),
    BACKGROUND_PRIORITY_UNAVAILABLE("background_priority_unavailable"),
    ANALYSIS_STORAGE_UNAVAILABLE("analysis_storage_unavailable"),
}

/** Full Binder/resource probes retain their one-second cadence; CPU is checked per frame. */
internal class QuietDecodeResourceGate(
    private val fullProbe: () -> QuietAnalysisReason?,
    private val cpuProbe: () -> QuietAnalysisReason?,
    private val monotonicMs: () -> Long,
) {
    private var lastFullProbe = monotonicMs()

    fun periodicRefusal(): QuietAnalysisReason? {
        val now = monotonicMs()
        if (now - lastFullProbe < 1_000) return null
        lastFullProbe = now
        return fullProbe()
    }

    fun beforeFrameRefusal(): QuietAnalysisReason? = cpuProbe()
    fun finalRefusal(): QuietAnalysisReason? = fullProbe()
}

/** Every decoded frame is checked for scene change and usable exposure/contrast. The known native
 * four-row timestamp overlay is excluded, never the foreground. This is not object recognition. */
internal class QuietFrameTracker {
    var frames = 0; private set
    var motionFrames = 0; private set
    var maxChangedPixels = 0; private set
    var maxCoherentPixels = 0; private set
    var maxRollingCoherentPixels = 0; private set
    var maxConsecutiveCoherentFrames = 0; private set
    var firstPts: Long? = null; private set
    var lastPts: Long? = null; private set
    var maxGapUs = 0L; private set
    var ptsValid = true; private set
    var minMeanLuma = 255.0; private set
    var maxMeanLuma = 0.0; private set
    var maxDarkFraction = 0.0; private set
    var maxBrightFraction = 0.0; private set
    var minContrastSpan = 255; private set
    val qualityValid: Boolean get() = frames > 0 && minMeanLuma >= 40 && maxMeanLuma <= 215 &&
        maxDarkFraction <= .55 && maxBrightFraction <= .4 && minContrastSpan >= 24
    private val history = ArrayDeque<IntArray>()
    private var coherentRun = 0

    private fun changed(now: IntArray, before: IntArray): Pair<Int, Int> {
        val mask = BooleanArray(QUIET_GRID_WIDTH * QUIET_ROI_ROWS) { abs(now[it] - before[it]) >= 12 }
        var coherent = 0
        // Sliding column sums match calibration's valid-interior 3x3 convolution, without
        // nine neighborhood lookups per grid cell on the recorder's limited CPU.
        val columns = IntArray(QUIET_GRID_WIDTH) { x -> (if (mask[x]) 1 else 0) +
            (if (mask[QUIET_GRID_WIDTH + x]) 1 else 0) }
        for (y in 0 until QUIET_ROI_ROWS) {
            if (y in 1 until QUIET_ROI_ROWS - 1) for (x in 1 until QUIET_GRID_WIDTH - 1) if (columns[x] +
                (if (x > 0) columns[x - 1] else 0) +
                (if (x + 1 < QUIET_GRID_WIDTH) columns[x + 1] else 0) >= 5) coherent++
            for (x in 0 until QUIET_GRID_WIDTH) {
                if (y > 0 && mask[(y - 1) * QUIET_GRID_WIDTH + x]) columns[x]--
                if (y + 2 < QUIET_ROI_ROWS && mask[(y + 2) * QUIET_GRID_WIDTH + x]) columns[x]++
            }
        }
        return mask.count { it } to coherent
    }

    fun accept(luma: IntArray, pts: Long) {
        require(luma.size == QUIET_GRID_WIDTH * QUIET_GRID_HEIGHT)
        if (pts < 0) ptsValid = false
        if (firstPts == null) firstPts = pts
        lastPts?.let { if (pts <= it) ptsValid = false; maxGapUs = max(maxGapUs, pts - it) }
        lastPts = pts
        val count = QUIET_GRID_WIDTH * QUIET_ROI_ROWS
        val histogram = IntArray(256)
        var sum = 0L; var dark = 0; var bright = 0
        for (i in 0 until count) {
            val value = luma[i]; require(value in 0..255)
            histogram[value]++; sum += value
            if (value < 20) dark++
            if (value > 235) bright++
        }
        fun percentile(fraction: Double): Int {
            val rank = kotlin.math.ceil(fraction * count).toInt().coerceAtLeast(1)
            var total = 0
            for (i in histogram.indices) { total += histogram[i]; if (total >= rank) return i }
            return 255
        }
        val mean = sum.toDouble() / count
        minMeanLuma = minOf(minMeanLuma, mean); maxMeanLuma = maxOf(maxMeanLuma, mean)
        maxDarkFraction = maxOf(maxDarkFraction, dark.toDouble() / count)
        maxBrightFraction = maxOf(maxBrightFraction, bright.toDouble() / count)
        minContrastSpan = minOf(minContrastSpan, percentile(.95) - percentile(.05))
        var adjacent = 0
        history.peekLast()?.let {
            val result = changed(luma, it)
            maxChangedPixels = max(maxChangedPixels, result.first); adjacent = result.second
        }
        val rolling = if (history.size == 6) changed(luma, checkNotNull(history.peekFirst())).second else 0
        coherentRun = if (adjacent >= 2) coherentRun + 1 else 0
        maxConsecutiveCoherentFrames = max(maxConsecutiveCoherentFrames, coherentRun)
        maxCoherentPixels = max(maxCoherentPixels, adjacent)
        maxRollingCoherentPixels = max(maxRollingCoherentPixels, rolling)
        if (coherentRun >= 2 || adjacent >= 16 || rolling >= 128) motionFrames++
        if (history.size == 6) history.removeFirst()
        history.addLast(luma.copyOf())
        frames++
    }

    fun proof(inputFrames: Int, inputEos: Boolean, decoderEos: Boolean, status: String? = null,
              reason: QuietAnalysisReason? = null): JSONObject {
        val duration = if (frames > 1) (checkNotNull(lastPts) - checkNotNull(firstPts)).coerceAtLeast(0) / 1_000 else 0L
        val complete = inputEos && decoderEos && ptsValid && inputFrames == frames && frames >= 20 &&
            duration in 30_000..120_000 && maxGapUs <= 150_000
        val result = status ?: when { !qualityValid && frames > 0 -> "unavailable"
            motionFrames > 0 -> "activity"; complete -> "quiet"; else -> "incomplete" }
        val diagnosis = reason ?: when (result) {
            "unavailable" -> if (!qualityValid && frames > 0) QuietAnalysisReason.FRAME_QUALITY else null
            "activity" -> QuietAnalysisReason.VISUAL_ACTIVITY
            "incomplete" -> QuietAnalysisReason.COVERAGE_INCOMPLETE
            "not_analyzed" -> QuietAnalysisReason.ANALYSIS_PENDING
            "error" -> QuietAnalysisReason.DECODER_FAILURE
            else -> null
        }
        return JSONObject().put("algorithm_version", QUIET_ALGORITHM_VERSION)
            .put("status", result).put("reason", diagnosis?.wire ?: JSONObject.NULL)
            .put("input_frames", inputFrames).put("decoded_frames", frames).put("sampled_frames", frames)
            .put("duration_ms", duration).put("covered_ms", duration).put("max_gap_ms", (maxGapUs + 999) / 1_000)
            .put("motion_frames", motionFrames)
            .put("analysis_completed_at_utc", Instant.ofEpochMilli(System.currentTimeMillis()).toString())
            .put("first_pts_us", firstPts ?: JSONObject.NULL).put("last_pts_us", lastPts ?: JSONObject.NULL)
            .put("input_eos", inputEos).put("decoder_eos", decoderEos)
            .put("grid_width", QUIET_GRID_WIDTH).put("grid_height", QUIET_GRID_HEIGHT)
            .put("max_changed_pixels", maxChangedPixels)
            .put("motion_metric", QUIET_MOTION_METRIC).put("rolling_frames", 6)
            .put("max_coherent_pixels", maxCoherentPixels).put("max_rolling_coherent_pixels", maxRollingCoherentPixels)
            .put("max_consecutive_coherent_frames", maxConsecutiveCoherentFrames)
            .put("roi_profile", QUIET_ROI_PROFILE).put("roi_rows", QUIET_ROI_ROWS)
            .put("min_mean_luma", if (frames > 0) minMeanLuma else 0).put("max_mean_luma", maxMeanLuma)
            .put("max_dark_fraction", maxDarkFraction).put("max_bright_fraction", maxBrightFraction)
            .put("min_contrast_span", if (frames > 0) minContrastSpan else 0)
    }
}

/** No frames are retained. Hardware-only, one bounded sequential read, on the indexer's IO worker.
 * Permission, thermal, memory, decode and coverage failures all preserve normal upload behavior. */
internal open class VisualQuietAnalyzer(private val context: Context) {
    internal open fun cpuResourceRefusal(phase: QuietThermalPhase = QuietThermalPhase.ADMISSION): QuietAnalysisReason? = runCatching {
        cpuTemperatureRefusal(File("/sys/class/thermal/thermal_zone0/type").readText().trim(),
            File("/sys/class/thermal/thermal_zone0/temp").readText().trim().toIntOrNull(), phase)
    }.getOrDefault(QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE)

    internal open fun batteryResourceRefusal(): QuietAnalysisReason? = runCatching {
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        batteryTemperatureRefusal(if (battery?.hasExtra("temperature") == true)
            battery.getIntExtra("temperature", Int.MIN_VALUE) else null)
    }.getOrDefault(QuietAnalysisReason.BATTERY_TEMPERATURE_UNAVAILABLE)

    internal open fun systemResourceRefusal(): QuietAnalysisReason? = runCatching {
        if (Build.VERSION.SDK_INT >= 29) {
            systemThermalRefusal(context.getSystemService(PowerManager::class.java)?.currentThermalStatus,
                QuietFirmwareThermalProfile.matches(Build.DISPLAY, Build.MODEL, Build.HARDWARE,
                    Build.VERSION.SDK_INT, Build.FINGERPRINT))
        } else null
    }.getOrDefault(QuietAnalysisReason.SYSTEM_THERMAL_UNAVAILABLE)

    internal open fun memoryResourceRefusal(): QuietAnalysisReason? = runCatching {
        val memory = ActivityManager.MemoryInfo()
        val activity = context.getSystemService(ActivityManager::class.java)
            ?: return@runCatching QuietAnalysisReason.MEMORY_PROBE_UNAVAILABLE
        activity.getMemoryInfo(memory)
        if (memory.lowMemory || memory.availMem < 128L * 1024 * 1024) QuietAnalysisReason.MEMORY_GUARD else null
    }.getOrDefault(QuietAnalysisReason.MEMORY_PROBE_UNAVAILABLE)

    internal open fun resourceRefusal(phase: QuietThermalPhase = QuietThermalPhase.ADMISSION): QuietAnalysisReason? {
        batteryResourceRefusal()?.let { return it }
        systemResourceRefusal()?.let { return it }
        memoryResourceRefusal()?.let { return it }
        return cpuResourceRefusal(phase)
    }

    fun resourcesAvailable(): Boolean = resourceRefusal() == null

    internal fun decodeResourceGate(monotonicMs: () -> Long = SystemClock::elapsedRealtime) = QuietDecodeResourceGate(
        { resourceRefusal(QuietThermalPhase.DECODING) },
        { cpuResourceRefusal(QuietThermalPhase.DECODING) }, monotonicMs,
    )

    internal fun acceptDecodedFrame(tracker: QuietFrameTracker, gate: QuietDecodeResourceGate, pts: Long,
                                    readLuma: () -> IntArray): QuietAnalysisReason? {
        gate.beforeFrameRefusal()?.let { return it }
        tracker.accept(readLuma(), pts)
        return null
    }

    internal fun finalProof(tracker: QuietFrameTracker, gate: QuietDecodeResourceGate, inputFrames: Int,
                            inputEos: Boolean, decoderEos: Boolean, allowed: () -> Boolean,
                            reason: QuietAnalysisReason? = null): JSONObject {
        if (decoderEos) {
            if (!allowed()) return tracker.proof(inputFrames, inputEos, false, "unavailable", QuietAnalysisReason.POLICY_NOT_PERMITTED)
            gate.finalRefusal()?.let {
                return tracker.proof(inputFrames, inputEos, false, "unavailable", it)
            }
        }
        return tracker.proof(inputFrames, inputEos, decoderEos, reason = reason)
    }

    open suspend fun analyze(uri: Uri, allowed: () -> Boolean): JSONObject {
        val tracker = QuietFrameTracker()
        var inputFrames = 0; var inputEos = false; var decoderEos = false
        var extractor: MediaExtractor? = null; var codec: MediaCodec? = null
        var failure = QuietAnalysisReason.SOURCE_UNAVAILABLE
        try {
            if (!allowed()) return tracker.proof(0, false, false, "unavailable", QuietAnalysisReason.POLICY_NOT_PERMITTED)
            resourceRefusal()?.let { return tracker.proof(0, false, false, "unavailable", it) }
            val coroutine = currentCoroutineContext()
            val started = SystemClock.elapsedRealtime()
            val resources = decodeResourceGate()
            extractor = MediaExtractor()
            context.contentResolver.openFileDescriptor(uri, "r").use { descriptor ->
                checkNotNull(descriptor)
                extractor.setDataSource(descriptor.fileDescriptor)
            }
            failure = QuietAnalysisReason.FORMAT_UNSUPPORTED
            val videoTracks = (0 until extractor.trackCount).map { it to extractor.getTrackFormat(it) }
                .filter { it.second.getString(MediaFormat.KEY_MIME)?.startsWith("video/") == true }
            check(videoTracks.size == 1)
            val (track, format) = videoTracks.single()
            val mime = checkNotNull(format.getString(MediaFormat.KEY_MIME))
            check(format.getInteger(MediaFormat.KEY_WIDTH) in 320..3840 &&
                format.getInteger(MediaFormat.KEY_HEIGHT) in 180..2160)
            val name = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos.firstOrNull { info ->
                !info.isEncoder && !(if (Build.VERSION.SDK_INT >= 29) info.isSoftwareOnly else
                    info.name.startsWith("OMX.google.") || info.name.startsWith("c2.android.")) &&
                    info.supportedTypes.any { it.equals(mime, true) } && runCatching {
                    val capabilities = info.getCapabilitiesForType(mime)
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible in capabilities.colorFormats &&
                        capabilities.isFormatSupported(format)
                }.getOrDefault(false)
            }?.name ?: return tracker.proof(0, false, false, "unavailable", QuietAnalysisReason.HARDWARE_DECODER_UNAVAILABLE)
            extractor.selectTrack(track)
            format.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Flexible)
            format.setInteger(MediaFormat.KEY_PRIORITY, 1)
            format.setFloat(MediaFormat.KEY_OPERATING_RATE, 60f)
            failure = QuietAnalysisReason.DECODER_FAILURE
            codec = MediaCodec.createByCodecName(name)
            codec.configure(format, null, null, 0)
            codec.start()
            val info = MediaCodec.BufferInfo()
            while (!decoderEos && SystemClock.elapsedRealtime() - started < 90_000 && tracker.frames < 7_200) {
                coroutine.ensureActive()
                if (!allowed()) return tracker.proof(inputFrames, inputEos, false, "unavailable", QuietAnalysisReason.POLICY_NOT_PERMITTED)
                resources.periodicRefusal()?.let { return tracker.proof(inputFrames, inputEos, false, "unavailable", it) }
                if (!inputEos) {
                    val index = codec.dequeueInputBuffer(5_000)
                    if (index >= 0) {
                        val buffer = checkNotNull(codec.getInputBuffer(index)).apply { clear() }
                        val size = extractor.readSampleData(buffer, 0)
                        if (size < 0) {
                            codec.queueInputBuffer(index, 0, 0, 0, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                            inputEos = true
                        } else {
                            check(extractor.sampleFlags and (MediaExtractor.SAMPLE_FLAG_ENCRYPTED or
                                MediaExtractor.SAMPLE_FLAG_PARTIAL_FRAME) == 0)
                            check(extractor.sampleTime >= 0)
                            codec.queueInputBuffer(index, 0, size, extractor.sampleTime, 0)
                            inputFrames++; extractor.advance()
                        }
                    }
                }
                val index = codec.dequeueOutputBuffer(info, 5_000)
                if (index >= 0) try {
                    if (info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        failure = QuietAnalysisReason.IMAGE_UNAVAILABLE
                        acceptDecodedFrame(tracker, resources, info.presentationTimeUs) {
                            checkNotNull(codec.getOutputImage(index)).use { image -> luma(image) }
                        }?.let {
                            return tracker.proof(inputFrames, inputEos, false, "unavailable", it)
                        }
                        failure = QuietAnalysisReason.DECODER_FAILURE
                        // Activity or unusable imagery already requires upload; stop wasting decoder/thermal budget.
                        if (tracker.motionFrames > 0 || !tracker.qualityValid) return tracker.proof(inputFrames, inputEos, false)
                    }
                    decoderEos = info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
                } finally { codec.releaseOutputBuffer(index, false) }
            }
            return finalProof(tracker, resources, inputFrames, inputEos, decoderEos, allowed, reason = when {
                decoderEos -> null
                tracker.frames >= 7_200 -> QuietAnalysisReason.FRAME_LIMIT
                else -> QuietAnalysisReason.ANALYSIS_TIMEOUT
            })
        } catch (error: Exception) {
            if (error is CancellationException) throw error
            return tracker.proof(inputFrames, inputEos, decoderEos, "error", failure)
        } finally {
            runCatching { codec?.stop() }; runCatching { codec?.release() }; runCatching { extractor?.release() }
        }
    }

    private fun luma(image: Image): IntArray {
        check(image.format == ImageFormat.YUV_420_888)
        val crop = image.cropRect
        check(crop.width() >= QUIET_GRID_WIDTH * 2 && crop.height() >= QUIET_GRID_HEIGHT * 2)
        val plane = image.planes.first(); val bytes = plane.buffer
        val row = plane.rowStride; val pixel = plane.pixelStride
        return IntArray(QUIET_GRID_WIDTH * QUIET_GRID_HEIGHT) { index ->
            val gy = index / QUIET_GRID_WIDTH; val gx = index % QUIET_GRID_WIDTH
            val y = (crop.top + (gy * 2L + 1) * crop.height() / (QUIET_GRID_HEIGHT * 2)).toInt().coerceAtMost(crop.bottom - 2)
            val x = (crop.left + (gx * 2L + 1) * crop.width() / (QUIET_GRID_WIDTH * 2)).toInt().coerceAtMost(crop.right - 2)
            val p = y * row + x * pixel
            ((bytes.get(p).toInt() and 255) + (bytes.get(p + pixel).toInt() and 255) +
                (bytes.get(p + row).toInt() and 255) + (bytes.get(p + row + pixel).toInt() and 255)) / 4
        }
    }

    companion object {
        internal fun cpuTemperatureRefusal(type: String?, milliC: Int?,
                                           phase: QuietThermalPhase = QuietThermalPhase.ADMISSION): QuietAnalysisReason? = when {
            type != "soc-thmzone" || milliC == null || milliC !in 10_000..150_000 -> QuietAnalysisReason.CPU_TEMPERATURE_UNAVAILABLE
            milliC >= 100_000 -> QuietAnalysisReason.CPU_TEMPERATURE_HARD_LIMIT
            phase == QuietThermalPhase.ADMISSION && milliC >= 95_000 -> QuietAnalysisReason.CPU_TEMPERATURE_GUARD
            else -> null
        }
        internal fun systemThermalRefusal(status: Int?, verifiedFirmware: Boolean = false): QuietAnalysisReason? = when {
            status == null || status !in PowerManager.THERMAL_STATUS_NONE..PowerManager.THERMAL_STATUS_SHUTDOWN -> QuietAnalysisReason.SYSTEM_THERMAL_UNAVAILABLE
            status >= PowerManager.THERMAL_STATUS_CRITICAL -> QuietAnalysisReason.SYSTEM_THERMAL_GUARD
            status == PowerManager.THERMAL_STATUS_SEVERE && !verifiedFirmware -> QuietAnalysisReason.SYSTEM_THERMAL_GUARD
            else -> null
        }
        internal fun batteryTemperatureRefusal(tenthsC: Int?): QuietAnalysisReason? = when {
            tenthsC == null || tenthsC !in -400..1_000 -> QuietAnalysisReason.BATTERY_TEMPERATURE_UNAVAILABLE
            tenthsC >= 420 -> QuietAnalysisReason.BATTERY_TEMPERATURE_GUARD
            else -> null
        }
        internal fun validCpuTemperature(type: String?, milliC: Int?): Boolean = cpuTemperatureRefusal(type, milliC) == null
    }
}

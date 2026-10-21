package com.dashcamstats.obdlogger

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.content.pm.ProviderInfo
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.provider.DocumentsContract
import androidx.test.core.app.ApplicationProvider
import java.io.File
import kotlinx.coroutines.test.runTest
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.shadows.ShadowContentResolver

@RunWith(RobolectricTestRunner::class)
class ClosedClipIndexerTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val tree = Uri.parse("content://movement.test/tree/ABCD-1234%3ADCIM")

    @Test fun actualSafReaderKeepsBothLensesLocksOrphansAndUnknownHistoryAndPublishesBoundedReport() = runTest {
        val files = File(context.cacheDir, "movement-provider").apply { mkdirs() }
        val provider = TestProvider(files)
        provider.attachInfo(context, ProviderInfo().apply { authority = "movement.test" })
        ShadowContentResolver.registerProviderInternal("movement.test", provider)
        context.getSharedPreferences(MOVEMENT_TREE_PREFERENCES, Context.MODE_PRIVATE).edit()
            .putString(MOVEMENT_TREE_URI, tree.toString()).commit()
        context.deleteDatabase("clip-movement.db")
        val journal = MotionJournal(context, backgroundScope)
        val root = File(context.cacheDir, "movement-report-root").apply { mkdirs() }
        val indexer = ClosedClipIndexer(context, journal, backgroundScope) { root }
        indexer.scan()
        var report = JSONObject(File(root, "movement/report.json").readText())
        assertEquals("ready", report.getString("status"))
        assertTrue(report.getBoolean("report_only"))
        assertTrue(report.getJSONArray("clips").toString().contains("file_unstable"))
        indexer.scan()
        report = JSONObject(File(root, "movement/report.json").readText())
        val array = report.getJSONArray("clips")
        val clips = (0 until array.length()).map { array.getJSONObject(it) }
        assertEquals(6, clips.size)
        assertEquals(setOf("camera_0", "camera_1"), clips.map { it.getString("camera_key") }.toSet())
        val locked = clips.single { it.getBoolean("locked") }
        assertTrue(locked.getString("relative_path").startsWith("DCIM/LockVideo/"))
        assertTrue(locked.getJSONArray("reasons").toString().contains("locked_clip"))
        assertTrue(clips.all { it.getString("classification") == "unknown" })
        val orphan = clips.single { it.getString("relative_path").startsWith("DCIM/pre_") }
        assertEquals("unknown", orphan.getString("time_quality"))
        assertTrue(orphan.isNull("duration_ms"))
        assertTrue(clips.all { it.isNull("duration_ms") })
        assertTrue(clips.any { !it.isNull("fingerprint") })
        assertTrue(File(root, "movement/report.json").length() <= MOVEMENT_REPORT_MAX_BYTES)
        assertFalse(File(root, "movement/report.json.partial").exists())
        // A real exporter-produced fixture for cross-language contract checks (no private data).
        File(System.getProperty("java.io.tmpdir"), "movement-report-v1-android.json")
            .writeText(report.toString(2))

        val before = clips.single { it.getString("relative_path") == "DCIM/Video/20261020190000_camera_0.ts" }
            .getJSONObject("fingerprint").getString("digest")
        // Same-size/mtime reuse still changes the sampled content identity when rechecked.
        File(files, "20261020190000_camera_0.ts").writeBytes(ByteArray(1024) { 0x48 })
        indexer.scan()
        val changed = JSONObject(File(root, "movement/report.json").readText()).getJSONArray("clips")
        val changedRows = (0 until changed.length()).map { changed.getJSONObject(it) }
        assertNotEquals(before, changedRows.single { it.getString("relative_path") == "DCIM/Video/20261020190000_camera_0.ts" }
            .getJSONObject("fingerprint").getString("digest"))
        provider.fillToCap = true
        indexer.scan()
        assertTrue(JSONObject(File(root, "movement/report.json").readText()).getBoolean("truncated"))

        provider.denied = true
        indexer.scan()
        report = JSONObject(File(root, "movement/report.json").readText())
        assertEquals("directory_unavailable", report.getString("status"))
        assertEquals(0, report.getJSONArray("clips").length())
        indexer.stop(); journal.stop(); journal.close()
    }

    @Test fun missingDirectoryGrantAndRemovableStorageDoNotDisableTheJournal() = runTest {
        MovementTreeAccess.revoke(context)
        val journal = MotionJournal(context, backgroundScope)
        val indexer = ClosedClipIndexer(context, journal, backgroundScope) { null }
        indexer.scan()
        assertEquals("storage_unavailable", indexer.state)
        assertFalse(journal.failed)
        indexer.stop(); journal.stop(); journal.close()
    }

    private class TestProvider(private val root: File) : ContentProvider() {
        var denied = false
        var fillToCap = false
        private val names = listOf("20261020190000_camera_0.ts", "20261020190100_camera_0.ts",
            "20261020190002_camera_1.ts", "20261020190102_camera_1.ts")
        private val locked = "20261020185900_camera_0.ts"
        private val orphan = "pre_20261020190200_camera_1.ts"
        init { (names + locked + orphan + "20260230120000_camera_0.ts").forEach { File(root, it).writeBytes(ByteArray(1024) { 0x47 }) } }
        override fun onCreate() = true
        override fun getType(uri: Uri) = "vnd.android.document/directory"
        override fun insert(uri: Uri, values: ContentValues?): Uri? = error("read only")
        override fun delete(uri: Uri, selection: String?, selectionArgs: Array<out String>?) = error("read only")
        override fun update(uri: Uri, values: ContentValues?, selection: String?, selectionArgs: Array<out String>?) = error("read only")
        override fun query(uri: Uri, projection: Array<out String>?, selection: String?,
                           selectionArgs: Array<out String>?, sortOrder: String?): Cursor {
            if (denied) throw SecurityException("permission revoked")
            val columns = projection ?: emptyArray()
            val cursor = MatrixCursor(columns)
            val id = DocumentsContract.getDocumentId(uri)
            val children = uri.lastPathSegment == "children"
            val paths = if (!children) listOf(id) else when (id) {
                "ABCD-1234:DCIM" -> listOf("ABCD-1234:DCIM/Video", "ABCD-1234:DCIM/LockVideo", "ABCD-1234:DCIM/$orphan")
                "ABCD-1234:DCIM/Video" -> names.map { "$id/$it" } + "$id/20260230120000_camera_0.ts" + "$id/20261020190500_camera_0.ts" +
                    if (fillToCap) (0..4096).map { "$id/unrecognized-$it.txt" } else emptyList()
                "ABCD-1234:DCIM/LockVideo" -> listOf("$id/$locked")
                else -> emptyList()
            }
            for (path in paths) {
                val name = path.substringAfterLast('/')
                val directory = name in listOf("Video", "LockVideo")
                val file = File(root, name)
                cursor.addRow(columns.map { column -> when (column) {
                    DocumentsContract.Document.COLUMN_DOCUMENT_ID -> path
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME -> name
                    DocumentsContract.Document.COLUMN_MIME_TYPE -> if (directory) DocumentsContract.Document.MIME_TYPE_DIR else "video/mp2t"
                    DocumentsContract.Document.COLUMN_SIZE -> if (file.exists()) file.length() else 0L
                    DocumentsContract.Document.COLUMN_LAST_MODIFIED -> 1_792_541_460_000L
                    else -> null
                } }.toTypedArray())
            }
            return cursor
        }
        override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
            check(mode == "r")
            return ParcelFileDescriptor.open(File(root, DocumentsContract.getDocumentId(uri).substringAfterLast('/')),
                ParcelFileDescriptor.MODE_READ_ONLY)
        }
    }
}

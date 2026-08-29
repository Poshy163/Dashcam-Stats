package com.dashcamstats.obdlogger

import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.io.File

@RunWith(RobolectricTestRunner::class)
class BundleRetentionTest {
    @get:Rule
    val temporaryFolder = TemporaryFolder()

    @Test
    fun missingBundleNeedsExactServerReceiptAndPresentBundleMustStillMatch() {
        val ready = temporaryFolder.newFolder("ready")
        val receipts = temporaryFolder.newFolder("receipts")
        File(ready, "present-valid.obd2.zip").writeText("valid")
        File(ready, "present-mismatch.obd2.zip").writeText("changed")
        File(ready, "present-invalid.obd2.zip").writeText("invalid")
        File(ready, "present-directory.obd2.zip").mkdir()
        val candidates = listOf(
            candidate("present-valid", "a"),
            candidate("present-mismatch", "b"),
            candidate("present-invalid", "c"),
            candidate("missing-no-receipt", "d"),
            candidate("exact-receipt", "e"),
            candidate("mismatch-receipt", "f"),
            candidate("malformed-receipt", "g"),
            candidate("present-directory", "h"),
        )
        writeReceipt(receipts, candidates[4])
        validateExactServerVerificationReceipt(
            File(receipts, "exact-receipt.verified.json"),
            receipts,
            candidates[4],
        )
        writeReceipt(receipts, candidates[5], bundleSha256 = digest("wrong"))
        File(receipts, "malformed-receipt.verified.json").writeText("not-json")

        val verified = retentionReceiptsSafeToPrune(
            candidates = candidates,
            maximumDeletes = 10,
            bundleFile = { File(ready, "$it.obd2.zip") },
            receiptFile = { File(receipts, "$it.verified.json") },
            validatePresentBundle = { file, _ -> check(file.readText() != "invalid") },
            hashPresentBundle = { file ->
                when (file.readText()) {
                    "valid" -> digest("a")
                    else -> digest("not-b")
                }
            },
            validateReceipt = { file, candidate ->
                isExactServerVerificationReceipt(file, receipts, candidate)
            },
        )

        assertEquals(listOf("present-valid", "exact-receipt"), verified.map { it.driveId })
    }

    @Test
    fun verificationAndDeletionSelectionAreBounded() {
        val ready = temporaryFolder.newFolder("empty")
        val receipts = temporaryFolder.newFolder("many-receipts")
        val candidates = (0..20).map { candidate("drive-$it", "$it") }
        candidates.forEach { writeReceipt(receipts, it) }
        val verified = retentionReceiptsSafeToPrune(
            candidates = candidates,
            maximumDeletes = 4,
            bundleFile = { File(ready, "$it.obd2.zip") },
            receiptFile = { File(receipts, "$it.verified.json") },
            validatePresentBundle = { _, _ -> error("no bundle files exist") },
            hashPresentBundle = { error("no bundle files exist") },
            validateReceipt = { file, candidate ->
                isExactServerVerificationReceipt(file, receipts, candidate)
            },
        )
        assertEquals(4, verified.size)
    }

    @Test
    fun receiptRejectsExtraKeysDuplicatesOversizeAndNonRegularPath() {
        val receipts = temporaryFolder.newFolder("strict-receipts")
        val candidate = candidate("drive-strict", "strict")
        val receipt = File(receipts, "drive-strict.verified.json")

        receipt.writeText(
            """{"schema_version":1,"drive_id":"drive-strict","bundle_sha256":"${candidate.bundleSha256}","extra":true}""",
        )
        assertEquals(false, isExactServerVerificationReceipt(receipt, receipts, candidate))
        receipt.writeText(
            """{"schema_version":1,"schema_version":1,"drive_id":"drive-strict","bundle_sha256":"${candidate.bundleSha256}"}""",
        )
        assertEquals(false, isExactServerVerificationReceipt(receipt, receipts, candidate))
        receipt.writeText(" ".repeat(513))
        assertEquals(false, isExactServerVerificationReceipt(receipt, receipts, candidate))
        receipt.delete()
        receipt.mkdir()
        assertEquals(false, isExactServerVerificationReceipt(receipt, receipts, candidate))
    }

    private fun candidate(id: String, digestSeed: String) = ExportedDriveRetentionCandidate(
        driveId = id,
        finishedAtUtc = "2025-01-01T00:00:00Z",
        bundleSha256 = digest(digestSeed),
    )

    private fun digest(seed: String): String =
        sha256(seed.toByteArray())

    private fun writeReceipt(
        root: File,
        candidate: ExportedDriveRetentionCandidate,
        bundleSha256: String = candidate.bundleSha256,
    ) {
        File(root, "${candidate.driveId}.verified.json").writeText(
            """{"schema_version":1,"drive_id":"${candidate.driveId}","bundle_sha256":"$bundleSha256"}""",
        )
    }
}

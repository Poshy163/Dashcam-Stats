package com.dashcamstats.obdlogger

import android.content.Context
import android.content.SharedPreferences
import android.app.Application
import android.os.Looper
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.TextView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows
import org.robolectric.annotation.LooperMode
import org.robolectric.annotation.Config
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@LooperMode(LooperMode.Mode.PAUSED)
@Config(application = MainActivityTestApplication::class)
class MainActivityTest {
    private lateinit var context: Context
    private lateinit var storage: ControlledTrialStorage
    private lateinit var repository: TrialSettingsRepository

    private class ControlledTrialStorage : TrialSettingsStorage {
        private var bytes: ByteArray? = null
        @Volatile var blockWrites = false
        @Volatile var failWrites = false
        @Volatile var writer: Thread? = null
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        override fun read() = bytes?.copyOf()
        override fun write(bytes: ByteArray) {
            writer = Thread.currentThread()
            if (blockWrites) {
                entered.countDown()
                check(release.await(5, TimeUnit.SECONDS))
            }
            check(!failWrites)
            this.bytes = bytes.copyOf()
        }
    }

    @Before fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE).edit().clear()
            .putBoolean("enabled", true).putBoolean("ownership_transferred", true)
            .putString("adapter_address", "AA:BB:CC:DD:EE:FF")
            .putString("vehicle_id", "vehicle").putString("logger_id", "logger")
            .putBoolean("webhook_enabled", false)
            // Deliberately unreadable: stopping must preserve it without touching Keystore.
            .putString(WebhookCredentialStore.ENCRYPTED_KEY, "unreadable-test-envelope").commit()
        storage = ControlledTrialStorage()
        var migrated: Int? = null
        var barrier: TrialSettingChange? = null
        repository = TrialSettingsRepository(storage, { LegacyTrialSetting(false, false, null) },
            { migrated }, { revision -> migrated = revision; true }, { true },
            { barrier }, { change -> barrier = change; true })
        repository.snapshot()
        @Suppress("UNCHECKED_CAST")
        val repositories = TrialSettings::class.java.getDeclaredField("repositories").apply { isAccessible = true }
            .get(TrialSettings) as MutableMap<String, TrialSettingsRepository>
        repositories.clear()
        repositories[File(context.noBackupFilesDir, "poll-trial-state-v1.json").absolutePath] = repository
    }

    @Test fun invalidDraftKeepsSavedLoggerRunningAndExplicitOffStillStopsWithoutSavingDraft() = withActivity { activity ->
        val preferences = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
        val before = preferences.all
        field<EditText>(activity, "voltageOn").setText("")
        save(activity)
        assertEquals(before, preferences.all)
        assertTrue(field<CheckBox>(activity, "enabled").isChecked)
        assertNull(Shadows.shadowOf(activity).nextStoppedService)
        assertTrue(field<TextView>(activity, "status").text.contains("Settings were not saved"))

        field<EditText>(activity, "address").setText("also invalid")
        field<EditText>(activity, "webhookUrl").setText("invalid URL")
        field<EditText>(activity, "webhookApiKey").setText("unsaved replacement")
        field<CheckBox>(activity, "enabled").isChecked = false
        save(activity)
        waitUntil { field<TextView>(activity, "status").text.startsWith("Logger disabled") }
        assertEquals(before + ("enabled" to false), preferences.all)
        assertEquals(ObdLoggerService::class.java.name,
            Shadows.shadowOf(activity).nextStoppedService.component?.className)
    }

    @Test fun invalidWebhookAndIdentityDraftsDoNotDisableTheSavedConfiguration() = withActivity { activity ->
        val preferences = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
        val before = preferences.all
        field<CheckBox>(activity, "webhookEnabled").isChecked = true
        field<EditText>(activity, "webhookApiKey").setText("temporary-test-key")
        field<EditText>(activity, "webhookUrl").setText("https://example.test/wrong-path")
        save(activity)
        assertEquals(before, preferences.all)
        assertTrue(field<TextView>(activity, "status").text.contains("Settings were not saved"))
        field<CheckBox>(activity, "webhookEnabled").isChecked = false
        field<EditText>(activity, "vehicleId").setText("not a valid id")
        save(activity)
        assertEquals(before, preferences.all)
        assertNull(Shadows.shadowOf(activity).nextStoppedService)
        assertTrue(field<CheckBox>(activity, "enabled").isChecked)
    }

    @Test fun credentialStorageFailureIsReportedWithoutStoppingOrClaimingSettingsWereSaved() = withActivity { activity ->
        val preferences = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
        val before = preferences.all
        // Robolectric has no Android Keystore provider; an explicit replacement must fail safely.
        field<EditText>(activity, "webhookApiKey").setText("replacement-test-key")
        save(activity)
        waitUntil { field<TextView>(activity, "status").text.contains("Settings were not saved") }
        assertEquals(before, preferences.all)
        assertNull(Shadows.shadowOf(activity).nextStoppedService)
        assertFalse(field<Boolean>(activity, "saving"))
    }

    @Test fun trialApplyDoesNotBlockUiWhileStorageIsBusyAndOnlyAppliesTheCapturedChoice() = withActivity { activity ->
        val checkbox = field<CheckBox>(activity, "pollTrial")
        val apply = field<Button>(activity, "pollTrialApply")
        checkbox.isChecked = true
        assertFalse(repository.snapshot().enabled)
        storage.blockWrites = true
        try {
            apply.performClick()
            assertTrue(storage.entered.await(2, TimeUnit.SECONDS))
            assertNotSame(Looper.getMainLooper().thread, storage.writer)
            assertFalse(apply.isEnabled)
            assertFalse(checkbox.isEnabled)
            // Rendering must use the cached immutable snapshot, even while the store lock is held.
            MainActivity::class.java.getDeclaredMethod("renderLiveStatus").apply { isAccessible = true }.invoke(activity)
            assertTrue(field<TextView>(activity, "pollTrialReason").text.contains("Saving trial"))
            apply.performClick() // A duplicate Apply cannot queue a second operation.
        } finally {
            storage.release.countDown()
        }
        waitUntil { !field<Boolean>(activity, "applyingTrial") }
        assertTrue(repository.snapshot().enabled)
        assertEquals(2, repository.snapshot().revision)
        assertTrue(checkbox.isChecked)
        assertFalse(apply.isEnabled)
        assertNull(Shadows.shadowOf(activity).nextStartedService)
    }

    @Test fun staleTrialApplyReloadsTheFallbackInsteadOfRearmingIt() = withActivity { activity ->
        field<CheckBox>(activity, "pollTrial").isChecked = true
        val fallback = repository.change(false, "transport_fault")
        field<Button>(activity, "pollTrialApply").performClick()
        waitUntil { !field<Boolean>(activity, "applyingTrial") }
        assertEquals(fallback, repository.snapshot())
        assertFalse(field<CheckBox>(activity, "pollTrial").isChecked)
        MainActivity::class.java.getDeclaredMethod("renderLiveStatus").apply { isAccessible = true }.invoke(activity)
        assertTrue(field<TextView>(activity, "pollTrialReason").text.contains("Review the current setting"))
        assertNull(Shadows.shadowOf(activity).nextStartedService)
    }

    @Test fun failedTrialWriteShowsAnErrorAndKeepsTheBaseline() = withActivity { activity ->
        field<CheckBox>(activity, "pollTrial").isChecked = true
        storage.failWrites = true
        field<Button>(activity, "pollTrialApply").performClick()
        waitUntil { !field<Boolean>(activity, "applyingTrial") }
        assertFalse(repository.snapshot().enabled)
        assertEquals("unavailable", repository.snapshot().storageState)
        MainActivity::class.java.getDeclaredMethod("renderLiveStatus").apply { isAccessible = true }.invoke(activity)
        assertTrue(field<TextView>(activity, "pollTrialReason").text.contains("Could not save"))
        assertTrue(field<Button>(activity, "pollTrialApply").isEnabled)
        assertNull(Shadows.shadowOf(activity).nextStartedService)
    }

    @Test fun destroyingTheFormDuringDisableCommitStillStopsTheLoggerAndPreservesOtherSettings() {
        val controller = Robolectric.buildActivity(MainActivity::class.java).create().resume()
        val activity = controller.get()
        val application = context as MainActivityTestApplication
        val gate = MainActivityCommitGate()
        var destroyed = false
        try {
            waitUntil { runCatching { field<CheckBox>(activity, "enabled") }.getOrNull() != null }
            val preferences = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
            val before = preferences.all
            application.commitGate = gate
            field<CheckBox>(activity, "enabled").isChecked = false
            save(activity)
            assertTrue(gate.entered.await(2, TimeUnit.SECONDS))
            controller.pause().stop().destroy()
            destroyed = true
            gate.release.countDown()
            waitUntil { !preferences.getBoolean("enabled", true) }
            var stopped: android.content.Intent? = null
            waitUntil {
                stopped = Shadows.shadowOf(context as Application).nextStoppedService
                stopped != null
            }
            assertEquals(ObdLoggerService::class.java.name, stopped?.component?.className)
            assertEquals(before + ("enabled" to false), preferences.all)
        } finally {
            gate.release.countDown()
            application.commitGate = null
            if (!destroyed) controller.pause().stop().destroy()
        }
    }

    @Test fun destroyingTheFormDuringSaveCommitStillReloadsTheSavedLogger() {
        val controller = Robolectric.buildActivity(MainActivity::class.java).create().resume()
        val activity = controller.get()
        val application = context as MainActivityTestApplication
        val gate = MainActivityCommitGate()
        var destroyed = false
        try {
            waitUntil { runCatching { field<EditText>(activity, "parkedIntervalSeconds") }.getOrNull() != null }
            Shadows.shadowOf(application).grantPermissions(android.Manifest.permission.BLUETOOTH_CONNECT,
                android.Manifest.permission.POST_NOTIFICATIONS)
            application.commitGate = gate
            field<EditText>(activity, "parkedIntervalSeconds").setText("60")
            save(activity)
            assertTrue(gate.entered.await(2, TimeUnit.SECONDS))
            controller.pause().stop().destroy()
            destroyed = true
            gate.release.countDown()
            val preferences = context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
            waitUntil { preferences.getLong("parked_interval_seconds", 30) == 60L }
            var started: android.content.Intent? = null
            waitUntil {
                started = Shadows.shadowOf(application).nextStartedService
                started != null
            }
            assertEquals(ObdLoggerService.ACTION_RELOAD_CONFIGURATION, started?.action)
            assertFalse(started?.getBooleanExtra(EXTRA_MOVEMENT_FROM_VISIBLE_ACTIVITY, true) ?: true)
            assertTrue(preferences.getBoolean("enabled", false))
        } finally {
            gate.release.countDown()
            application.commitGate = null
            if (!destroyed) controller.pause().stop().destroy()
        }
    }

    @Test fun aServiceRequestFailureAfterCommitDoesNotClaimTheSettingsWereNotSaved() = withActivity { activity ->
        val application = context as MainActivityTestApplication
        Shadows.shadowOf(application).grantPermissions(android.Manifest.permission.BLUETOOTH_CONNECT,
            android.Manifest.permission.POST_NOTIFICATIONS)
        application.failServiceRequests = true
        try {
            field<EditText>(activity, "parkedIntervalSeconds").setText("60")
            save(activity)
            waitUntil { field<TextView>(activity, "status").text.contains("Settings saved, but") }
            assertEquals(60L, context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE)
                .getLong("parked_interval_seconds", 30))
            assertFalse(field<TextView>(activity, "status").text.contains("sensitive-platform"))
        } finally {
            application.failServiceRequests = false
        }
    }

    @Test fun asynchronousPermissionOrGpsStartupReportsAServiceRequestFailureWithoutCrashing() = withActivity { activity ->
        val application = context as MainActivityTestApplication
        application.failServiceRequests = true
        try {
            MainActivity::class.java.getDeclaredMethod("startLogger").apply { isAccessible = true }.invoke(activity)
            waitUntil { field<TextView>(activity, "status").text.contains("could not start") }
            assertTrue(context.getSharedPreferences("obd_logger", Context.MODE_PRIVATE).getBoolean("enabled", false))
            assertFalse(field<TextView>(activity, "status").text.contains("sensitive-platform"))
        } finally {
            application.failServiceRequests = false
        }
    }

    private fun withActivity(test: (MainActivity) -> Unit) {
        val controller = Robolectric.buildActivity(MainActivity::class.java).create().resume()
        val activity = controller.get()
        try {
            waitUntil { runCatching { field<CheckBox>(activity, "pollTrial") }.getOrNull() != null }
            test(activity)
        } finally {
            storage.release.countDown()
            controller.pause().stop().destroy()
        }
    }

    private fun save(activity: MainActivity) {
        MainActivity::class.java.getDeclaredMethod("saveAndApply").apply { isAccessible = true }.invoke(activity)
    }

    private inline fun <reified T> field(activity: MainActivity, name: String): T =
        MainActivity::class.java.getDeclaredField(name).apply { isAccessible = true }.get(activity) as T

    private fun waitUntil(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (true) {
            Shadows.shadowOf(Looper.getMainLooper()).idle()
            if (condition()) return
            check(System.nanoTime() < deadline) { "Activity did not finish its asynchronous operation" }
            Thread.sleep(10)
        }
    }
}

/** Blocks only the actual settings commit, after the Activity has finished loading. */
class MainActivityCommitGate {
    val entered = CountDownLatch(1)
    val release = CountDownLatch(1)
    fun awaitCommit() {
        entered.countDown()
        check(release.await(5, TimeUnit.SECONDS))
    }
}

class MainActivityTestApplication : Application() {
    @Volatile var commitGate: MainActivityCommitGate? = null
    @Volatile var failServiceRequests = false
    override fun startService(service: android.content.Intent): android.content.ComponentName? {
        checkServiceRequest()
        return super.startService(service)
    }
    override fun startForegroundService(service: android.content.Intent): android.content.ComponentName? {
        checkServiceRequest()
        return super.startForegroundService(service)
    }
    private fun checkServiceRequest() {
        if (failServiceRequests) throw SecurityException("sensitive-platform-start-error")
    }
    override fun getSharedPreferences(name: String, mode: Int): SharedPreferences {
        val preferences = super.getSharedPreferences(name, mode)
        if (name != "obd_logger") return preferences
        return object : SharedPreferences by preferences {
            override fun edit(): SharedPreferences.Editor {
                val editor = preferences.edit()
                // Every fluent mutator must keep returning the proxy, otherwise chained
                // putBoolean(...).commit() would bypass the controlled commit entirely.
                return java.lang.reflect.Proxy.newProxyInstance(
                    SharedPreferences.Editor::class.java.classLoader,
                    arrayOf(SharedPreferences.Editor::class.java),
                ) { proxy, method, arguments ->
                    if (method.name == "commit") commitGate?.awaitCommit()
                    val result = method.invoke(editor, *(arguments ?: emptyArray()))
                    if (method.returnType == SharedPreferences.Editor::class.java) proxy else result
                } as SharedPreferences.Editor
            }
        }
    }
}

package com.dashcamstats.obdlogger

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.net.Uri
import android.provider.Settings
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

class MainActivity : Activity() {
    private lateinit var address: EditText
    private lateinit var vehicleId: EditText
    private lateinit var loggerId: EditText
    private lateinit var voltageOn: EditText
    private lateinit var voltageOff: EditText
    private lateinit var offGraceSeconds: EditText
    private lateinit var parkedIntervalSeconds: EditText
    private lateinit var voltageOnlyMode: CheckBox
    private lateinit var pollTrial: CheckBox
    private lateinit var pollTrialReason: TextView
    private lateinit var pollTrialApply: Button
    private lateinit var pollTrialDraft: TrialSettingsDraft
    private lateinit var webhookEnabled: CheckBox
    private lateinit var webhookUrl: EditText
    private lateinit var webhookApiKey: EditText
    private lateinit var backupAwakeSeconds: EditText
    private lateinit var idleAwakeSeconds: EditText
    private lateinit var ownership: CheckBox
    private lateinit var enabled: CheckBox
    private lateinit var status: TextView
    private lateinit var liveStatus: TextView
    private lateinit var metricsToggle: Button
    private lateinit var liveMetrics: TextView
    private lateinit var credentialError: TextView
    private val activityScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var refreshJob: Job? = null
    private var foreground = false
    private var metricsExpanded = false
    private var publishedStatus: JSONObject? = null
    private var credentialLoadError: String? = null
    private var saving = false
    private var renderingPollTrial = false
    private var applyingTrial = false
    private var trialRefreshGeneration = 0L
    private var trialApplyNotice: String? = null
    private var trialNoticeSnapshot: TrialSettingsSnapshot? = null
    private lateinit var storedTrial: TrialSettingsSnapshot
    private lateinit var savedConfiguration: LoggerConfig

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // The head unit's screen is the only place this status is visible; the activity is
        // opened on purpose and should not dim while someone is reading it.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContentView(TextView(this).apply { text = "Loading logger configuration…" })
        activityScope.launch {
            val initial = withContext(Dispatchers.IO) {
                runCatching {
                    LoggerPreferences.load(this@MainActivity) to LoggerPreferences.pollTrialSnapshot(this@MainActivity)
                }.getOrNull()
            }
            if (initial == null) {
                setContentView(TextView(this@MainActivity).apply {
                    text = "Logger configuration could not be loaded. Reopen the app to try again."
                })
                return@launch
            }
            savedConfiguration = initial.first
            storedTrial = initial.second
            createConfigurationForm(initial.first)
            if (foreground) startStatusRefresh()
        }
    }

    private fun createConfigurationForm(current: LoggerConfig) {
        credentialLoadError = current.webhookCredentialError
        val padding = (16 * resources.displayMetrics.density).toInt()
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }
        layout.addView(TextView(this).apply {
            text = "Dashcam OBD logger"
            textSize = 24f
        })
        liveStatus = TextView(this).apply {
            textSize = 18f
            typeface = Typeface.MONOSPACE
            setTextColor(Color.WHITE)
            setBackgroundColor(Color.rgb(24, 24, 28))
            setPadding(padding, padding, padding, padding)
            setLineSpacing(0f, 1.2f)
        }
        layout.addView(
            liveStatus,
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
        )
        liveMetrics = TextView(this).apply {
            textSize = 16f
            typeface = Typeface.MONOSPACE
            setPadding(padding, padding, padding, padding)
            visibility = View.GONE
        }
        metricsToggle = Button(this).apply {
            text = "Show metrics and sleep details"
            setOnClickListener {
                metricsExpanded = !metricsExpanded
                renderLiveStatus()
            }
        }
        layout.addView(metricsToggle)
        layout.addView(liveMetrics)
        layout.addView(TextView(this).apply {
            text = "On-camera movement report. The optional quiet-footage filter below controls transfer; files stay on the card. " +
                "Choose the recorder's DCIM folder to summarise closed clips. GPS is optional; " +
                "only speed, uncertainty and coverage are stored, never coordinates."
        })
        layout.addView(Button(this).apply {
            text = "Choose DCIM folder (read only)"
            setOnClickListener {
                runCatching {
                    startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT_TREE).addFlags(
                        Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION), 201)
                }.onFailure { status.text = "The system folder picker is unavailable; logging and backup continue." }
            }
        })
        layout.addView(Button(this).apply {
            text = "Remove footage folder access"
            setOnClickListener { MovementTreeAccess.revoke(this@MainActivity); status.text = "Folder access removed." }
        })
        layout.addView(CheckBox(this).apply {
            text = "Skip quiet parked footage during backup (off by default)"
            isChecked = getSharedPreferences(MOVEMENT_TREE_PREFERENCES, MODE_PRIVATE)
                .getBoolean(QUIET_FILTER_ENABLED, false)
            setOnCheckedChangeListener { _, checked ->
                getSharedPreferences(MOVEMENT_TREE_PREFERENCES, MODE_PRIVATE).edit()
                    .putBoolean(QUIET_FILTER_ENABLED, checked).apply()
                status.text = if (checked) "Quiet filtering enabled. Both lenses require complete visual and ignition evidence; uncertain footage uploads."
                    else "Quiet filtering disabled. Normal backup includes all footage."
            }
        })
        layout.addView(TextView(this).apply {
            text = "Experimental quiet filtering compares every decoded frame for scene change on the camera. It retains driving, " +
                "ignition transitions, locked clips, detected activity, unusable light and uncertain analysis. Small or static objects " +
                "may not be recognised; enable only after reviewing the camera's results. Analysis pauses during " +
                "driving, backup, heat or low memory. Pending analysis may delay transfer by at most six minutes; " +
                "activity and uncertain results upload normally. No footage is deleted."
        })
        layout.addView(CheckBox(this).apply {
            text = "Use optional passive GPS speed (no active GPS requests)"
            isChecked = getSharedPreferences(MOVEMENT_TREE_PREFERENCES, MODE_PRIVATE)
                .getBoolean(MOVEMENT_GPS_ENABLED, false)
            setOnCheckedChangeListener { _, checked ->
                getSharedPreferences(MOVEMENT_TREE_PREFERENCES, MODE_PRIVATE).edit()
                    .putBoolean(MOVEMENT_GPS_ENABLED, checked).apply()
                if (checked) requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION), 101)
                else startLogger()
            }
        })
        layout.addView(Button(this).apply {
            text = "Optional GPS after reboot: app location settings"
            setOnClickListener {
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:$packageName")))
            }
        })
        layout.addView(TextView(this).apply {
            text = "Before enabling: turn off switch.nissan_tiida_obd2_connection and stop all phone OBD apps. The adapter permits one owner."
        })
        address = field(layout, "BLE adapter address", current.adapterAddress).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS
        }
        vehicleId = field(layout, "Vehicle ID", current.vehicleId)
        loggerId = field(layout, "Logger ID", current.loggerId)
        voltageOn = decimalField(layout, "Engine-on voltage (10.0–16.0 V)", current.voltageOn)
        voltageOff = decimalField(layout, "Engine-off voltage (10.0–16.0 V)", current.voltageOff)
        offGraceSeconds = field(
            layout,
            "Engine-off grace (0–300 seconds)",
            current.offGraceSeconds.toString(),
        ).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        parkedIntervalSeconds = field(
            layout,
            "Parked voltage interval (15–3600 seconds)",
            current.parkedIntervalSeconds.toString(),
        ).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        voltageOnlyMode = CheckBox(this).apply {
            text = "Controlled voltage-only mode (ATRV only; never initialise the ECU)"
            isChecked = current.voltageOnlyMode
        }
        layout.addView(voltageOnlyMode)
        pollTrial = CheckBox(this).apply {
            text = "Trial: 3-second speed / RPM / MAF (applies next drive; automatic fallback)"
            isChecked = current.pollTrialEnabled
        }
        pollTrialDraft = TrialSettingsDraft(storedTrial)
        layout.addView(pollTrial)
        pollTrialReason = TextView(this).apply {
            text = current.pollTrialFallbackReason?.let { "${PollTrialPlan.fallbackText(it)} Check the box to re-arm." } ?: "Baseline polling is the default."
        }
        layout.addView(pollTrialReason)
        pollTrialApply = Button(this).apply {
            text = "Apply trial setting"
            setOnClickListener { applyTrialSetting() }
        }
        layout.addView(pollTrialApply)
        pollTrial.setOnCheckedChangeListener { _, checked ->
            if (renderingPollTrial) return@setOnCheckedChangeListener
            trialApplyNotice = null
            trialNoticeSnapshot = null
            pollTrialDraft.select(checked)
            renderTrialSetting()
        }
        webhookEnabled = CheckBox(this).apply {
            text = "Send webhook to Dashcam Analyser on engine-off / sleep"
            isChecked = current.webhookEnabled
        }
        layout.addView(webhookEnabled)
        webhookUrl = field(
            layout,
            "Webhook URL (e.g. http://192.168.1.16:8199/api/ingest/webhook)",
            current.webhookUrl,
        )
        webhookApiKey = field(layout, "Webhook API key (X-API-Key)", current.webhookApiKey).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        credentialError = TextView(this).apply {
            setTextColor(Color.rgb(180, 30, 30))
        }
        layout.addView(credentialError)
        showCredentialError(credentialLoadError)
        backupAwakeSeconds = field(
            layout,
            "Backup awake time (Wi-Fi connected, seconds)",
            current.backupAwakeSeconds.toString(),
        ).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        idleAwakeSeconds = field(
            layout,
            "Idle awake time (non-Wi-Fi, seconds)",
            current.idleAwakeSeconds.toString(),
        ).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
        }
        ownership = CheckBox(this).apply {
            text = "I explicitly transferred adapter ownership from every other BLE client"
            isChecked = current.ownershipTransferred
        }
        enabled = CheckBox(this).apply {
            text = "Start logger now and after boot"
            isChecked = current.enabled
        }
        layout.addView(ownership)
        layout.addView(enabled)
        status = TextView(this)
        layout.addView(Button(this).apply {
            text = "Save and apply"
            setOnClickListener { saveAndApply() }
        })
        layout.addView(status)
        setContentView(ScrollView(this).apply { addView(layout) })
        renderLiveStatus()
    }

    override fun onResume() {
        super.onResume()
        foreground = true
        MovementActivityVisibility.visible = true
        if (::liveStatus.isInitialized) startStatusRefresh()
    }

    override fun onPause() {
        foreground = false
        MovementActivityVisibility.visible = false
        refreshJob?.cancel()
        refreshJob = null
        super.onPause()
    }

    override fun onDestroy() {
        activityScope.cancel()
        super.onDestroy()
    }

    private fun startStatusRefresh() {
        refreshJob?.cancel()
        refreshJob = activityScope.launch {
            while (isActive) {
                val generation = trialRefreshGeneration
                val refreshed = withContext(Dispatchers.IO) {
                    val roots = listOfNotNull(
                        runCatching { DeviceFiles.removableRootOrNull(this@MainActivity) }.getOrNull(),
                        runCatching { DeviceFiles.fallbackStatusRoot(this@MainActivity) }.getOrNull(),
                    )
                    LiveStatusSummary.readPublishedStatus(roots, System.currentTimeMillis()) to
                        LoggerPreferences.pollTrialSnapshot(this@MainActivity)
                }
                publishedStatus = refreshed.first
                // A refresh started before Apply must not replace its newer result.
                if (!applyingTrial && generation == trialRefreshGeneration) storedTrial = refreshed.second
                renderLiveStatus()
                delay(LIVE_STATUS_REFRESH_MILLIS)
            }
        }
    }

    private fun renderLiveStatus() {
        renderTrialSetting()
        liveStatus.text = LiveStatusSummary.lines(publishedStatus, System.currentTimeMillis())
            .joinToString("\n")
        liveMetrics.visibility = if (metricsExpanded) View.VISIBLE else View.GONE
        metricsToggle.text = if (metricsExpanded) "Hide metrics and sleep details"
            else "Show metrics and sleep details"
        if (metricsExpanded) {
            liveMetrics.text = LiveStatusSummary.detailLines(publishedStatus).joinToString("\n")
        }
    }

    private fun renderTrialSetting() {
        val stored = storedTrial
        if (trialNoticeSnapshot != stored) trialApplyNotice = null
        pollTrialDraft.refresh(stored)
        renderingPollTrial = true
        pollTrial.isChecked = pollTrialDraft.selected
        renderingPollTrial = false
        pollTrial.isEnabled = !applyingTrial
        pollTrialApply.isEnabled = !applyingTrial && (pollTrialDraft.dirty || stored.storageState == "unavailable")
        pollTrialReason.text = when {
            applyingTrial -> "Saving trial setting…"
            trialApplyNotice != null -> trialApplyNotice
            stored.storageState == "unavailable" -> "The saved trial setting is unavailable. Baseline is selected; explicitly apply your choice to recover."
            pollTrialDraft.dirty && pollTrialDraft.base.token != stored.token ->
                "Trial state changed while this choice was pending. Apply will reload the current state for review."
            pollTrialDraft.dirty -> "Selection pending. Tap Apply trial setting to save it for the next drive."
            stored.fallbackReason != null -> "${PollTrialPlan.fallbackText(stored.fallbackReason)} Check the box and apply to re-arm."
            stored.enabled -> "Trial armed for the next drive; current plan ${publishedStatus?.optInt("poll_plan_version", 7) ?: 7}."
            else -> "Baseline selected for the next drive."
        }
    }

    private fun applyTrialSetting() {
        if (applyingTrial) return
        val selected = pollTrialDraft.selected
        val expected = pollTrialDraft.base
        applyingTrial = true
        trialApplyNotice = null
        trialNoticeSnapshot = null
        trialRefreshGeneration++
        renderTrialSetting()
        activityScope.launch {
            val outcome = withContext(Dispatchers.IO) {
                val repository = TrialSettings.repository(this@MainActivity)
                try {
                    TrialApplyOutcome(repository.change(selected, null, expected))
                } catch (error: Exception) {
                    if (error is CancellationException) throw error
                    TrialApplyOutcome(runCatching { repository.snapshot() }.getOrNull(), error)
                }
            }
            applyingTrial = false
            trialRefreshGeneration++
            outcome.snapshot?.let { storedTrial = it }
            if (outcome.error == null || outcome.error is StaleTrialSettingsException) {
                pollTrialDraft.reload(storedTrial)
            }
            trialApplyNotice = when (outcome.error) {
                null -> null
                is StaleTrialSettingsException ->
                    "Trial state changed. Review the current setting, choose again and apply."
                else -> "Could not save the trial setting. Review the current state and retry Apply when storage is available."
            }
            trialNoticeSnapshot = storedTrial.takeIf { trialApplyNotice != null }
            renderTrialSetting()
        }
    }

    private data class TrialApplyOutcome(val snapshot: TrialSettingsSnapshot?, val error: Exception? = null)

    private fun showCredentialError(message: String?) {
        credentialError.text = when (message) {
            WEBHOOK_CREDENTIAL_UNAVAILABLE ->
                "The saved webhook API key is unavailable. Re-enter it to restore webhook delivery. Existing logging continues."
            else -> message.orEmpty()
        }
        credentialError.visibility = if (message == null) View.GONE else View.VISIBLE
    }

    private fun field(parent: LinearLayout, hint: String, value: String): EditText {
        return EditText(this).also {
            it.hint = hint
            it.setText(value)
            parent.addView(
                it,
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            )
        }
    }

    private fun decimalField(parent: LinearLayout, hint: String, value: Double): EditText =
        field(parent, hint, value.toString()).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
        }

    private fun saveAndApply() {
        if (saving) return
        if (!enabled.isChecked) {
            disableSavedLogger()
            return
        }
        val config = LoggerConfig(
            enabled = enabled.isChecked,
            ownershipTransferred = ownership.isChecked,
            adapterAddress = address.text.toString().trim(),
            vehicleId = vehicleId.text.toString().trim(),
            loggerId = loggerId.text.toString().trim(),
            voltageOn = voltageOn.text.toString().toDoubleOrNull() ?: Double.NaN,
            voltageOff = voltageOff.text.toString().toDoubleOrNull() ?: Double.NaN,
            offGraceSeconds = offGraceSeconds.text.toString().toLongOrNull() ?: -1,
            parkedIntervalSeconds = parkedIntervalSeconds.text.toString().toLongOrNull() ?: -1,
            voltageOnlyMode = voltageOnlyMode.isChecked,
            pollTrialEnabled = storedTrial.enabled,
            pollTrialFallbackReason = storedTrial.fallbackReason,
            webhookEnabled = webhookEnabled.isChecked,
            webhookUrl = webhookUrl.text.toString().trim(),
            webhookApiKey = webhookApiKey.text.toString().trim(),
            webhookCredentialError = credentialLoadError.takeIf { webhookApiKey.text.isBlank() },
            backupAwakeSeconds = backupAwakeSeconds.text.toString().toIntOrNull() ?: -1,
            idleAwakeSeconds = idleAwakeSeconds.text.toString().toIntOrNull() ?: -1,
        )
        if (config.enabled && config.webhookEnabled && config.webhookCredentialError != null) {
            status.text = "Re-enter the webhook API key to save these settings. Existing logging continues."
            showCredentialError(config.webhookCredentialError)
            return
        }
        if (!config.thresholdConfigurationValid) {
            rejectConfiguration(
                "Use 10.0–16.0 V, engine-off below engine-on, 0–300 seconds grace, " +
                    "15–3600 second parked interval, 30–3600s backup awake, and 15–3600s idle awake",
            )
            return
        }
        // Stopping collection must remain possible while the webhook credential is unavailable.
        val webhookError = if (config.enabled) webhookConfigurationError(config) else null
        when (webhookError) {
            "webhook_api_key_required" -> rejectConfiguration("Enter the webhook API key")
            "webhook_api_key_too_long" -> rejectConfiguration("Webhook API key is too long")
            "invalid_webhook_url" -> rejectConfiguration(
                "Use the /api/ingest/webhook HTTPS endpoint, or HTTP with a private LAN IP address",
            )
            null -> Unit
            else -> rejectConfiguration("Webhook configuration is invalid")
        }
        if (webhookError != null) return
        if (config.enabled && config.ownershipTransferred && !config.canRun) {
            rejectConfiguration("A valid adapter address, vehicle ID and logger ID are required")
            return
        }
        saving = true
        status.text = "Saving settings…"
        val appContext = applicationContext
        activityScope.launch {
            val applied = try {
                // Persistence and its runtime action are one operation. Closing the form must
                // not leave a committed setting unapplied after a slow disk write completes.
                withContext(NonCancellable + Dispatchers.IO) {
                    LoggerPreferences.save(appContext, config)
                    when {
                        !config.ownershipTransferred -> RuntimeApplyOutcome(
                            actionFailed = runCatching {
                                stopSavedLogger(appContext, config.ownershipTransferred)
                            }.isFailure,
                        )
                        !hasLoggerPermissions(appContext) -> RuntimeApplyOutcome(needsPermissions = true)
                        else -> RuntimeApplyOutcome(actionFailed = runCatching { reloadSavedLogger(appContext) }.isFailure)
                    }
                }
            } catch (error: WebhookCredentialStorageException) {
                showCredentialError(error.message)
                status.text = "Settings were not saved. Unlock the device and re-enter the webhook API key to try again. Existing logging continues."
                return@launch
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                status.text = "Settings were not saved. Retry when storage is available. Existing logging continues."
                return@launch
            } finally {
                saving = false
            }
            credentialLoadError = config.webhookCredentialError
            savedConfiguration = config
            showCredentialError(credentialLoadError)
            if (applied.actionFailed) {
                status.text = "Settings saved, but the logger could not be updated. Reopen the app and retry Save and apply."
            } else if (applied.needsPermissions) {
                requestNeededPermissions()
            } else {
                status.text = if (config.ownershipTransferred) "Logger starting" else "Logger disabled"
            }
        }
    }

    private fun rejectConfiguration(message: String) {
        // Invalid edits are only a draft; the last saved configuration keeps running.
        status.text = "$message. Settings were not saved; existing logging continues."
    }

    private fun disableSavedLogger() {
        saving = true
        status.text = "Stopping logger…"
        val appContext = applicationContext
        val ownershipTransferred = savedConfiguration.ownershipTransferred
        activityScope.launch {
            val stopped = try {
                // Do not rewrite invalid drafts or an unreadable webhook credential just to stop.
                withContext(NonCancellable + Dispatchers.IO) {
                    LoggerPreferences.disablePersisted(appContext)
                    runCatching { stopSavedLogger(appContext, ownershipTransferred) }.isSuccess
                }
            } catch (error: Exception) {
                if (error is CancellationException) throw error
                status.text = "Could not save the disabled setting. Existing logging continues; retry when storage is available."
                return@launch
            } finally {
                saving = false
            }
            savedConfiguration = savedConfiguration.copy(enabled = false)
            status.text = if (stopped) "Logger disabled. Other unsaved edits were not applied."
                else "Disabled setting saved, but the logger could not be stopped. Retry Save and apply."
        }
    }

    private data class RuntimeApplyOutcome(val needsPermissions: Boolean = false, val actionFailed: Boolean = false)

    private fun stopSavedLogger(context: Context, ownershipTransferred: Boolean) {
        context.stopService(Intent(context, ObdLoggerService::class.java))
        runCatching { StatusPublisher.publish(context, PublicStatus("disabled", ownershipTransferred)) }
    }

    private fun reloadSavedLogger(context: Context) {
        val service = Intent(context, ObdLoggerService::class.java).setAction(
            ObdLoggerService.ACTION_RELOAD_CONFIGURATION,
        ).putExtra(EXTRA_MOVEMENT_FROM_VISIBLE_ACTIVITY, MovementActivityVisibility.visible)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) context.startForegroundService(service)
        else context.startService(service)
    }

    private fun requestNeededPermissions() {
        val wanted = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            wanted += Manifest.permission.BLUETOOTH_CONNECT
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        if (wanted.any { checkSelfPermission(it) != android.content.pm.PackageManager.PERMISSION_GRANTED }) {
            requestPermissions(wanted.toTypedArray(), 100)
        } else {
            startLogger()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 101) {
            startLogger()
            status.text = "Passive GPS is optional. Missing permission leaves ACC and OBD movement reporting active."
            return
        }
        if (requestCode == 100 && hasLoggerPermissions(this)) {
            startLogger()
        } else if (requestCode == 100) {
            LoggerPreferences.disable(this)
            enabled.isChecked = false
            runCatching {
                StatusPublisher.error(
                    this,
                    ownership.isChecked,
                    "permission_required",
                    "Nearby Devices permission was denied; logger remains disabled",
                )
            }
            status.text = "Nearby Devices and notification permissions are required; logger disabled"
        }
    }

    private fun startLogger() {
        activityScope.launch {
            val config = withContext(Dispatchers.IO) {
                runCatching { LoggerPreferences.load(this@MainActivity, includeWebhookCredential = false) }.getOrNull()
            }
            if (config == null) {
                status.text = "Saved settings could not be loaded. Retry when storage is available."
                return@launch
            }
            if (!config.canRun) {
                status.text = "Ownership, valid adapter address, vehicle ID and logger ID are required"
                return@launch
            }
            status.text = if (runCatching { reloadSavedLogger(applicationContext) }.isSuccess) "Logger starting"
                else "Saved settings loaded, but the logger could not start. Reopen the app and retry Save and apply."
        }
    }

    @Deprecated("Activity result API remains supported on the head unit")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != 201 || resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        runCatching { MovementTreeAccess.save(this, uri, data.flags) }
            .onSuccess { status.text = "Read-only DCIM access saved. Closed clips are indexed within one minute." }
            .onFailure { status.text = "Select the recorder's DCIM folder and allow read access. Logging and backup continue." }
    }

    private companion object {
        const val LIVE_STATUS_REFRESH_MILLIS = 2_000L
    }
}

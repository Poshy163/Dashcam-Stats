package com.dashcamstats.obdlogger

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.os.SystemClock
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeout
import java.util.UUID

open class ElmException(message: String) : RuntimeException(message)
class ElmCommandRejectedException(message: String) : ElmException(message)
data class DtcQueryResult(val codes: List<String>, val status: String)

@SuppressLint("MissingPermission")
class ElmBleClient(private val context: Context, private val address: String) {
    private val serviceUuid = UUID.fromString("0000fff0-0000-1000-8000-00805f9b34fb")
    private val characteristicUuid = UUID.fromString("0000fff1-0000-1000-8000-00805f9b34fb")
    private val cccdUuid = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    private val commandMutex = Mutex()
    private val commandSession = ElmCommandSession()
    private var gatt: BluetoothGatt? = null
    private var characteristic: BluetoothGattCharacteristic? = null
    private var connection: CompletableDeferred<Unit>? = null
    private var response: CompletableDeferred<String>? = null
    private var lastCommandAt = 0L
    var ecuSource: Int? = null
        private set

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS || newState == BluetoothProfile.STATE_DISCONNECTED) {
                commandSession.taint()
                val error = ElmException("BLE disconnected (status $status); adapter may have another owner")
                connection?.completeExceptionally(error)
                response?.completeExceptionally(error)
                return
            }
            if (newState == BluetoothProfile.STATE_CONNECTED && !gatt.discoverServices()) {
                connection?.completeExceptionally(ElmException("could not start GATT discovery"))
            }
        }

        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                connection?.completeExceptionally(ElmException("GATT discovery failed ($status)"))
                return
            }
            val service: BluetoothGattService? = gatt.getService(serviceUuid)
            val found = service?.getCharacteristic(characteristicUuid)
            if (found == null || found.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY == 0 ||
                found.properties and BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE == 0
            ) {
                connection?.completeExceptionally(ElmException("required FFF0/FFF1 notify+write channel absent"))
                return
            }
            characteristic = found
            if (!gatt.setCharacteristicNotification(found, true)) {
                connection?.completeExceptionally(ElmException("could not enable FFF1 notifications"))
                return
            }
            val descriptor = found.getDescriptor(cccdUuid)
            if (descriptor == null) {
                connection?.completeExceptionally(ElmException("FFF1 has no notification descriptor"))
                return
            }
            descriptor.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            if (!gatt.writeDescriptor(descriptor)) {
                connection?.completeExceptionally(ElmException("could not write FFF1 notification descriptor"))
            }
        }

        override fun onDescriptorWrite(gatt: BluetoothGatt, descriptor: BluetoothGattDescriptor, status: Int) {
            if (descriptor.uuid == cccdUuid && status == BluetoothGatt.GATT_SUCCESS) {
                connection?.complete(Unit)
            } else if (descriptor.uuid == cccdUuid) {
                connection?.completeExceptionally(ElmException("FFF1 notification setup failed ($status)"))
            }
        }

        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) = accept(value)

        @Deprecated("pre-API 33 callback")
        override fun onCharacteristicChanged(gatt: BluetoothGatt, characteristic: BluetoothGattCharacteristic) {
            accept(characteristic.value ?: return)
        }
    }

    private fun accept(fragment: ByteArray) {
        val pending = response ?: return
        when (val assembled = commandSession.accept(fragment)) {
            is ElmResponseAssembly.Complete -> pending.complete(assembled.response)
            ElmResponseAssembly.Overflow -> pending.completeExceptionally(
                ElmException("ELM response exceeded 8192 bytes"),
            )
            ElmResponseAssembly.Ignored,
            ElmResponseAssembly.Pending,
            -> Unit
        }
    }

    suspend fun connect() {
        if (!hasBluetoothPermissions(context)) throw ElmException("Nearby Devices permission is missing")
        val manager = context.getSystemService(BluetoothManager::class.java)
        val adapter = manager?.adapter ?: throw ElmException("Bluetooth is unavailable")
        if (!adapter.isEnabled) throw ElmException("Bluetooth is disabled")
        val device: BluetoothDevice = try {
            adapter.getRemoteDevice(address)
        } catch (error: IllegalArgumentException) {
            throw ElmException("configured adapter address is invalid")
        }
        connection = CompletableDeferred()
        gatt = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
        try {
            withTimeout(20_000) { connection!!.await() }
            commandSession.freshConnection()
        } catch (_: TimeoutCancellationException) {
            disconnect(false)
            throw ElmException("BLE connection timed out; adapter may have another owner")
        } catch (cancelled: CancellationException) {
            disconnect(false)
            throw cancelled
        } catch (error: Exception) {
            disconnect(false)
            throw error
        }
    }

    suspend fun command(value: String, timeoutMillis: Long = 6_000): String = commandMutex.withLock {
        val command = ElmProtocol.normalize(value)
        if (!ElmProtocol.isSafe(command)) throw ElmException("unsafe OBD command refused: $command")
        val activeGatt = gatt ?: throw ElmException("BLE is not connected")
        val target = characteristic ?: throw ElmException("FFF1 is unresolved")
        val since = SystemClock.elapsedRealtime() - lastCommandAt
        if (since < 100) delay(100 - since)
        commandSession.beginCommand()
        response = CompletableDeferred()
        target.writeType = BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        target.value = "$command\r".toByteArray(Charsets.US_ASCII)
        try {
            if (!activeGatt.writeCharacteristic(target)) {
                commandSession.taint()
                throw ElmException("FFF1 write was rejected")
            }
            commandSession.awaitResponse(response!!, command, timeoutMillis)
        } finally {
            lastCommandAt = SystemClock.elapsedRealtime()
            response = null
        }
    }

    suspend fun initialize(): Double? {
        val commands = listOf(
            "ATZ", "ATI", "ATD", "ATD0", "ATE0", "ATL0", "ATH1", "ATSP0", "ATE0",
            "ATH1", "ATM0", "ATS0", "ATAT1", "ATAL", "ATST64",
        )
        for (command in commands) command(command, if (command == "ATZ") 12_000 else 6_000)
        return ElmProtocol.voltage(command("ATRV"))
    }

    suspend fun proveEcu(): Set<Int> {
        command("ATSP0")
        val reply = command("0100", 35_000)
        if (reply.uppercase().contains("NO DATA") || ElmProtocol.hasTransportError(reply)) {
            commandSession.taint()
            throw ElmException("ECU did not answer 0100")
        }
        val (source, payload) = ElmProtocol.payload(reply, "0100", 0, 4)
            ?: run {
                commandSession.taint()
                throw ElmException("0100 failed ISO length/source/checksum validation")
            }
        ecuSource = source
        val mask = payload.fold(0L) { value, byte -> (value shl 8) or (byte.toLong() and 0xFF) }
        val supported = (1..32).filter { mask and (1L shl (32 - it)) != 0L }.toMutableSet()
        if (0x20 in supported) {
            val extensionReply = command("0120")
            if (extensionReply.uppercase().contains("NO DATA")) {
                throw ElmException("ECU advertised 0120 but returned NO DATA")
            }
            if (ElmProtocol.hasTransportError(extensionReply)) {
                throw ElmException("transport error for 0120")
            }
            rejectNegative(extensionReply, "0120", 0x01, source)
            val extension = parseOrTaint {
                ElmProtocol.requirePayload(
                    extensionReply,
                    "0120",
                    0x20,
                    4,
                    source,
                ).second
            }
            val extensionMask = extension.fold(0L) { value, byte ->
                (value shl 8) or (byte.toLong() and 0xFF)
            }
            supported += (1..32).filter {
                extensionMask and (1L shl (32 - it)) != 0L
            }.map { 0x20 + it }
        }
        return supported
    }

    suspend fun queryProtocolNumber(): String? = ElmProtocol.protocolNumber(command("ATDPN"))

    suspend fun query(pid: Int): Map<String, Any> {
        val payload = queryPayload(pid) ?: return emptyMap()
        return ElmProtocol.decode(pid, payload)
    }

    suspend fun queryPayload(pid: Int): ByteArray? {
        val length = ElmProtocol.pidLengths[pid] ?: return null
        val command = "01%02X".format(pid)
        val reply = command(command)
        if (reply.uppercase().contains("NO DATA")) return null
        if (ElmProtocol.hasTransportError(reply)) throw ElmException("transport error for $command")
        val source = ecuSource ?: throw ElmException("ECU source is not learned")
        rejectNegative(reply, command, 0x01, source)
        // command() only returns after receiving the next ELM prompt, so the command stream is
        // synchronized even when this optional PID's ISO frame is malformed. Reject the value
        // without tainting the connection; the poller records it as missing and continues. A
        // missing prompt, overflow, failed write or disconnect is still tainted in command().
        return ElmProtocol.requirePayload(reply, command, pid, length, source).second
    }

    suspend fun queryDtcs(mode: Int): List<String> {
        return queryDtcsWithStatus(mode).codes
    }

    suspend fun queryDtcsWithStatus(mode: Int): DtcQueryResult {
        require(mode in setOf(0x03, 0x07, 0x0A))
        val source = ecuSource ?: throw ElmException("ECU source is not learned")
        val command = "%02X".format(mode)
        val reply = command(command)
        if (reply.uppercase().contains("NO DATA")) {
            return DtcQueryResult(emptyList(), "no_data")
        }
        if (ElmProtocol.hasTransportError(reply)) throw ElmException("transport error for $command")
        rejectNegative(reply, command, mode, source)
        return DtcQueryResult(
            ElmProtocol.dtcs(
                ElmProtocol.requireModePayloads(reply, command, mode, source),
            ),
            "ok",
        )
    }

    suspend fun queryMode09Supported(): Set<Int> {
        val source = ecuSource ?: throw ElmException("ECU source is not learned")
        val reply = command("0900")
        if (reply.uppercase().contains("NO DATA")) return emptySet()
        if (ElmProtocol.hasTransportError(reply)) throw ElmException("transport error for 0900")
        rejectNegative(reply, "0900", 0x09, source)
        return ElmProtocol.mode09Supported(reply, source)
            ?: throw ElmProtocolException(
                "0900 reply failed ISO header/source/checksum validation",
            )
    }

    suspend fun queryMode09Count(pid: Int): Int? {
        require(pid in setOf(0x03, 0x05))
        val source = ecuSource ?: throw ElmException("ECU source is not learned")
        val command = "09%02X".format(pid)
        val reply = command(command)
        if (reply.uppercase().contains("NO DATA")) return null
        if (ElmProtocol.hasTransportError(reply)) throw ElmException("transport error for $command")
        rejectNegative(reply, command, 0x09, source)
        return ElmProtocol.mode09Count(reply, pid, source)
            ?: throw ElmProtocolException("$command reply failed ISO count validation")
    }

    suspend fun queryCalibrationId(): String? {
        val source = ecuSource ?: throw ElmException("ECU source is not learned")
        val reply = command("0904")
        if (reply.uppercase().contains("NO DATA")) return null
        if (ElmProtocol.hasTransportError(reply)) throw ElmException("transport error for 0904")
        rejectNegative(reply, "0904", 0x09, source)
        return ElmProtocol.mode09Text(reply, "0904", 4, source)
            ?: throw ElmProtocolException("0904 reply failed ISO framing or sequence validation")
    }

    suspend fun queryCalibrationVerificationNumbers(): List<String> {
        val source = ecuSource ?: throw ElmException("ECU source is not learned")
        val reply = command("0906")
        if (reply.uppercase().contains("NO DATA")) return emptyList()
        if (ElmProtocol.hasTransportError(reply)) throw ElmException("transport error for 0906")
        rejectNegative(reply, "0906", 0x09, source)
        return ElmProtocol.mode09Cvns(reply, source).takeIf { it.isNotEmpty() }
            ?: throw ElmProtocolException("0906 reply failed ISO framing or sequence validation")
    }

    suspend fun queryFreezeFramePayload(pid: Int, length: Int, frame: Int = 0): ByteArray? {
        require(pid in ElmProtocol.freezeFramePids || pid == 0x00 || pid == 0x02)
        require(frame == 0) { "only confirmed freeze-frame number 0 is read" }
        val source = ecuSource ?: throw ElmException("ECU source is not learned")
        val command = "02%02X%02X".format(pid, frame)
        val reply = command(command)
        if (reply.uppercase().contains("NO DATA")) return null
        if (ElmProtocol.hasTransportError(reply)) throw ElmException("transport error for $command")
        rejectNegative(reply, command, 0x02, source)
        val payload = ElmProtocol.requirePayload(
            reply,
            command,
            pid,
            length + 1,
            source,
            mode = 0x02,
        ).second
        if (payload.firstOrNull()?.toInt()?.and(0xFF) != frame) {
            throw ElmProtocolException("$command reply carried the wrong freeze-frame number")
        }
        return payload.drop(1).toByteArray()
    }

    private fun rejectNegative(reply: String, command: String, mode: Int, source: Int) {
        ElmProtocol.negativeResponse(reply, command, mode, source)?.let { nrc ->
            throw ElmCommandRejectedException("ECU rejected $command with NRC %02X".format(nrc))
        }
    }

    private inline fun <T> parseOrTaint(operation: () -> T): T = try {
        operation()
    } catch (error: ElmProtocolException) {
        commandSession.taint()
        throw error
    }

    suspend fun readVoltage(): Double? = ElmProtocol.voltage(command("ATRV"))

    suspend fun disconnect(closeProtocol: Boolean = true) {
        if (closeProtocol && gatt != null && !commandSession.isTainted) {
            try {
                command("ATPC")
            } catch (_: Exception) {
                // BLE close below is authoritative.
            }
        }
        gatt?.disconnect()
        gatt?.close()
        gatt = null
        characteristic = null
        ecuSource = null
        commandSession.disconnected()
    }

    fun closeNow() {
        gatt?.disconnect()
        gatt?.close()
        gatt = null
        characteristic = null
        ecuSource = null
        commandSession.disconnected()
    }
}

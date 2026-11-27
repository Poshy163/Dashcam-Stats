package com.dashcamstats.obdlogger;

import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothClass;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.content.Context;
import android.media.AudioManager;

import androidx.test.core.app.ApplicationProvider;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.util.ReflectionHelpers;

import java.util.List;
import java.util.Set;
import java.util.concurrent.Callable;

import static android.bluetooth.BluetoothProfile.*;
import static com.dashcamstats.obdlogger.CarPlayPhoneBluetooth.UNKNOWN_PEER_STATE;
import static org.junit.Assert.assertEquals;
import static org.robolectric.Shadows.shadowOf;

@RunWith(RobolectricTestRunner.class)
public class CarPlayPhoneBluetoothTest {
    private final CarPlayPhoneBluetooth helper = new CarPlayPhoneBluetooth();
    private final FakeProfile hfp = new FakeProfile();
    private final FakeProfile sink = new FakeProfile();
    private Context context;
    private BluetoothAdapter adapter;
    private BluetoothDevice phone;
    private BluetoothDevice other;
    private static int handle;
    private static boolean connected;

    public static int classicHandle(int transport) {
        assertEquals(BluetoothDevice.TRANSPORT_BREDR, transport);
        return handle;
    }
    public static boolean deviceConnected() { return connected; }

    public static class FakeProfile implements BluetoothProfile {
        int state = STATE_DISCONNECTED;
        List<BluetoothDevice> peers = List.of();
        List<BluetoothDevice> changing = List.of();
        List<String> calls = List.of();
        boolean playing;
        int audioState;

        @Override public int getConnectionState(BluetoothDevice device) { return state; }
        @Override public List<BluetoothDevice> getConnectedDevices() { return peers; }
        @Override public List<BluetoothDevice> getDevicesMatchingConnectionStates(int[] states) { return changing; }
        public List<String> getCurrentCalls(BluetoothDevice device) { return calls; }
        public int getAudioState(BluetoothDevice device) { return audioState; }
        public boolean isAudioPlaying(BluetoothDevice device) { return playing; }
    }

    @Before public void setUp() {
        context = ApplicationProvider.getApplicationContext();
        adapter = context.getSystemService(BluetoothManager.class).getAdapter();
        shadowOf(adapter).setState(BluetoothAdapter.STATE_ON);
        phone = adapter.getRemoteDevice("12:34:56:78:90:AB");
        other = adapter.getRemoteDevice("12:34:56:78:90:AC");
        BluetoothClass smartphone = ReflectionHelpers.callConstructor(BluetoothClass.class,
                ReflectionHelpers.ClassParameter.from(int.class, BluetoothClass.Device.PHONE_SMART));
        for (BluetoothDevice device : List.of(phone, other)) {
            shadowOf(device).setType(BluetoothDevice.DEVICE_TYPE_DUAL);
            shadowOf(device).setBondState(BluetoothDevice.BOND_BONDED);
            shadowOf(device).setBluetoothClass(smartphone);
        }
        shadowOf(adapter).setBondedDevices(Set.of(phone));
        helper.onServiceConnected(16, hfp);
        helper.onServiceConnected(11, sink);
        handle = BluetoothDevice.ERROR;
        connected = false;
    }

    private int peer() throws Exception {
        return helper.waitingPeer(context, adapter, phone,
                getClass().getMethod("classicHandle", int.class), getClass().getMethod("deviceConnected"));
    }

    private void connectHeadset() {
        handle = 17;
        connected = true;
        hfp.state = STATE_CONNECTED;
        hfp.peers = List.of(phone);
    }

    private String attempt(Callable<Boolean> fresh, Callable<Boolean> claim, StringBuilder actions) throws Exception {
        return CarPlayPhoneBluetooth.actIfStillWaiting(this::peer, fresh, claim,
                () -> { actions.append("connect"); return "reconnect_requested"; },
                () -> { actions.append("cycle"); return "reconnect_requested"; });
    }

    @Test public void disconnectedPhoneConnectsOnceAndConnectedIdlePhoneCyclesOnce() throws Exception {
        StringBuilder actions = new StringBuilder();
        assertEquals("waiting_connect_requested", attempt(() -> true, () -> true, actions));
        assertEquals("connect", actions.toString());
        connectHeadset();
        actions.setLength(0);
        assertEquals("waiting_cycle_requested", attempt(() -> true, () -> true, actions));
        assertEquals("cycle", actions.toString());
    }

    @Test public void ambiguousPeerUnsettledProfilesOrUnknownAclNeverAuthorizeConnect() throws Exception {
        shadowOf(adapter).setBondedDevices(Set.of(phone, other));
        assertEquals(UNKNOWN_PEER_STATE, peer());
        shadowOf(adapter).setBondedDevices(Set.of(phone));
        sink.changing = List.of(other);
        assertEquals(UNKNOWN_PEER_STATE, peer());
        sink.changing = List.of();
        hfp.state = STATE_CONNECTING;
        assertEquals(UNKNOWN_PEER_STATE, peer());
        hfp.state = STATE_DISCONNECTED;
        sink.peers = List.of(other);
        assertEquals(UNKNOWN_PEER_STATE, peer());
        sink.peers = List.of();
        connected = true;
        assertEquals(UNKNOWN_PEER_STATE, peer());
        connected = false;
        handle = 0xFFFF;
        assertEquals(UNKNOWN_PEER_STATE, peer());
    }

    @Test public void callsAudioInUseAndDisconnectedProfilesWithLiveAclBlockCycle() throws Exception {
        connectHeadset();
        hfp.calls = List.of("call");
        assertEquals(UNKNOWN_PEER_STATE, peer());
        hfp.calls = List.of();
        hfp.audioState = 2;
        assertEquals(UNKNOWN_PEER_STATE, peer());
        hfp.audioState = 0;
        sink.playing = true;
        assertEquals(UNKNOWN_PEER_STATE, peer());
        sink.playing = false;
        hfp.state = STATE_DISCONNECTED;
        hfp.peers = List.of();
        assertEquals(UNKNOWN_PEER_STATE, peer());
        connected = false;
        handle = BluetoothDevice.ERROR;
        shadowOf(context.getSystemService(AudioManager.class)).setIsMusicActive(true);
        assertEquals(UNKNOWN_PEER_STATE, peer());
    }

    @Test public void newLinkOrNewHandleDuringScreenCheckLeavesReconnectAlone() throws Exception {
        StringBuilder actions = new StringBuilder();
        Callable<Boolean> unexpectedClaim = () -> { throw new AssertionError("no mutation should be claimed"); };
        assertEquals("peer_changed", attempt(() -> { connectHeadset(); return true; }, unexpectedClaim, actions));
        assertEquals("peer_changed", attempt(() -> { handle++; return true; }, unexpectedClaim, actions));
        assertEquals("", actions.toString());
    }

    @Test public void screenChangeBusyPhoneOrSpentIgnitionClaimBlocksBothActions() throws Exception {
        StringBuilder actions = new StringBuilder();
        assertEquals("no_longer_waiting", attempt(() -> false, () -> true, actions));
        assertEquals("wake_or_gate_changed", attempt(() -> true, () -> false, actions));
        connectHeadset();
        assertEquals("peer_changed", attempt(() -> { hfp.calls = List.of("call"); return true; }, () -> true, actions));
        assertEquals("", actions.toString());
    }
}

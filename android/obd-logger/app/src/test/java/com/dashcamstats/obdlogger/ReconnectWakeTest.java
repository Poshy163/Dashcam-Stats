package com.dashcamstats.obdlogger;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.nio.channels.FileChannel;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.concurrent.TimeUnit;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import static org.junit.Assert.*;

public class ReconnectWakeTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder();

    /** File locks belonging to this JVM are not equivalent to a separate app_process. */
    public static class Owner {
        public static void main(String[] args) throws Exception {
            try (ReconnectWake wake = ReconnectWake.open(Paths.get(args[0]))) {
                System.out.println(wake.token);
                System.in.read();
            }
        }
    }

    private static Process owner(Path root) throws Exception {
        String tests = Paths.get(Owner.class.getProtectionDomain().getCodeSource().getLocation().toURI()).toString();
        String main = Paths.get(ReconnectWake.class.getProtectionDomain().getCodeSource().getLocation().toURI()).toString();
        return new ProcessBuilder(Paths.get(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp", tests + File.pathSeparator + main, Owner.class.getName(), root.toString()).start();
    }

    private static String token(Process process) throws Exception {
        return new BufferedReader(new InputStreamReader(process.getInputStream())).readLine();
    }

    @Test public void oneClaimPerLiveWakeAndNoConcurrentOperationAcrossWakes() throws Exception {
        Path root = temporary.newFolder().toPath();
        Process first = owner(root);
        try {
            String old = token(first);
            assertTrue(ReconnectWake.isCurrent(root, old));
            assertFalse(ReconnectWake.isCurrent(root, "../reconnect.operation"));
            try (FileChannel inFlight = ReconnectWake.claim(root, old)) {
                assertNotNull(inFlight);
                first.destroyForcibly();
                assertTrue(first.waitFor(5, TimeUnit.SECONDS));
                long until = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
                while (ReconnectWake.isCurrent(root, old) && System.nanoTime() < until) Thread.sleep(10);
                assertFalse(ReconnectWake.isCurrent(root, old));
                ReconnectWake.cleanOrphans(root);
                Process second = owner(root);
                try {
                    String next = token(second);
                    assertNotEquals(old, next);
                    assertNull(ReconnectWake.claim(root, next));
                    inFlight.close(); // Both the prior operation and its restorative reconnect have finished.
                    try (FileChannel accepted = ReconnectWake.claim(root, next)) {
                        assertNotNull(accepted);
                    }
                    assertNull(ReconnectWake.claim(root, next));
                    second.getOutputStream().close();
                    assertTrue(second.waitFor(5, TimeUnit.SECONDS));
                    assertFalse(ReconnectWake.isCurrent(root, next));
                } finally { second.destroyForcibly(); }
            }
        } finally { first.destroyForcibly(); }
    }
}

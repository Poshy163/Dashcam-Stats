package com.dashcamstats.obdlogger;

import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.UUID;

/** A helper must present a token whose owning ignition watcher is still alive. */
final class ReconnectWake implements AutoCloseable {
    private static final Path ROOT = Paths.get("/data/local/tmp/dashcam_wifi_startup");
    private static final String PREFIX = "reconnect.wake.";
    private static FileChannel operation;
    final String token;
    private final Path file;
    private final FileChannel owner;

    private ReconnectWake(Path root) throws IOException {
        token = UUID.randomUUID().toString();
        file = root.resolve(PREFIX + token);
        owner = FileChannel.open(file, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
        try {
            owner.lock();
        } catch (IOException | RuntimeException error) {
            close();
            throw error;
        }
    }

    static ReconnectWake open(Path root) throws IOException { return new ReconnectWake(root); }

    @Override public void close() throws IOException {
        try { owner.close(); } finally {
            Files.deleteIfExists(file);
            Files.deleteIfExists(claimFile(file));
        }
    }

    private static Path claimFile(Path file) {
        return file.resolveSibling(file.getFileName() + ".claimed");
    }

    boolean wasClaimed() { return !Files.notExists(claimFile(file), LinkOption.NOFOLLOW_LINKS); }

    static boolean validToken(String token) {
        if (token == null || token.length() != 36) return false;
        try { return UUID.fromString(token).toString().equals(token); }
        catch (IllegalArgumentException invalid) { return false; }
    }

    /** The caller holds the controller's exclusive watch.lock. Interrupted watchers
     * can leave an empty file behind, but process death releases its lock immediately. */
    static void cleanOrphans(Path root) throws IOException {
        try (DirectoryStream<Path> entries = Files.newDirectoryStream(root, PREFIX + "*")) {
            for (Path path : entries) {
                String name = path.getFileName().toString().substring(PREFIX.length());
                if (name.endsWith(".claimed")) name = name.substring(0, name.length() - 8);
                if (validToken(name)
                        && Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) Files.deleteIfExists(path);
            }
        }
    }

    static boolean isCurrent(String token) { return isCurrent(ROOT, token); }

    static boolean isCurrent(Path root, String token) {
        if (!validToken(token)) return false;
        try (FileChannel channel = FileChannel.open(root.resolve(PREFIX + token),
                StandardOpenOption.READ, LinkOption.NOFOLLOW_LINKS)) {
            // Two simultaneous verifiers must never be mistaken for the exclusive owner.
            try (FileLock acquired = channel.tryLock(0, Long.MAX_VALUE, true)) {
                return acquired == null; // Only the live watcher holds this token file.
            }
        } catch (IOException | RuntimeException unavailable) { return false; }
    }

    /** The one-shot app_process keeps this lock through both the action and any
     * restorative reconnect, until process exit. No two generations can act concurrently. */
    static synchronized boolean claim(String token) {
        if (operation != null) return false;
        operation = claim(ROOT, token);
        return operation != null;
    }

    static FileChannel claim(Path root, String token) {
        if (!isCurrent(root, token)) return null;
        FileChannel channel = null;
        boolean keep = false;
        try {
            channel = FileChannel.open(root.resolve("reconnect.operation"), StandardOpenOption.CREATE,
                    StandardOpenOption.WRITE, LinkOption.NOFOLLOW_LINKS);
            if (channel.tryLock() == null || !isCurrent(root, token)) return null;
            // CREATE_NEW consumes this generation even if its first helper crashes
            // before replying. A fresh ignition gets a different token file.
            Files.createFile(claimFile(root.resolve(PREFIX + token)));
            keep = isCurrent(root, token);
            return keep ? channel : null;
        } catch (IOException | OverlappingFileLockException unavailable) { return null; }
        finally {
            if (!keep && channel != null) {
                try { channel.close(); } catch (IOException ignored) { }
            }
        }
    }
}

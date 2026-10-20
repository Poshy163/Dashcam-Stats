# Android companion signing

The existing production signing files are local to the `C:\Github\Dashcam-Stats`
checkout:

- Keystore: `android/obd-logger/secrets/dashcam-obd-prod.p12`.
- Build credentials: `android/obd-logger/keystore.properties`.
- The properties file sets `obdReleaseKeystorePath=secrets/dashcam-obd-prod.p12`,
  resolved relative to `android/obd-logger` by Gradle.

Both files are Git-ignored and excluded from Docker build contexts. Keep their
contents out of commits, logs and chat. Fresh clones and worktrees do not contain
these files; check the existing checkout instead of generating a replacement key.

Reuse this production key for companion updates. Its certificate SHA-256 is
`e9191adb8f04a7117d71763c5e1332e7c0e4639a63afa09aef9c9bb1684d408d`.
Keep an encrypted offline recovery backup of the key and its credentials.

Gradle command-line properties and `OBD_RELEASE_*` environment variables override
the local properties file. Use `OBD_PRODUCTION_SIGNING=true` for production builds
and verify the resulting APK certificate before an in-place update. See
`docs/obd-dashcam-logger.md` for the build and deployment procedure.

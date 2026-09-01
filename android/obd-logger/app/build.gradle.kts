import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val localSigningProperties = Properties()
val localSigningPropertiesFile = rootProject.file("keystore.properties")
if (localSigningPropertiesFile.isFile) {
    localSigningPropertiesFile.inputStream().use(localSigningProperties::load)
}

fun releaseSigningValue(propertyName: String, environmentName: String): String? =
    providers.gradleProperty(propertyName).orNull?.trim()?.takeIf(String::isNotEmpty)
        ?: providers.environmentVariable(environmentName).orNull?.trim()?.takeIf(String::isNotEmpty)
        ?: localSigningProperties.getProperty(propertyName)?.trim()?.takeIf(String::isNotEmpty)

val releaseKeystorePath = releaseSigningValue("obdReleaseKeystorePath", "OBD_RELEASE_KEYSTORE_PATH")
val releaseKeystorePassword =
    releaseSigningValue("obdReleaseKeystorePassword", "OBD_RELEASE_KEYSTORE_PASSWORD")
val releaseKeyAlias = releaseSigningValue("obdReleaseKeyAlias", "OBD_RELEASE_KEY_ALIAS")
val releaseKeyPassword = releaseSigningValue("obdReleaseKeyPassword", "OBD_RELEASE_KEY_PASSWORD")
val releaseSigningInputs = listOf(
    releaseKeystorePath,
    releaseKeystorePassword,
    releaseKeyAlias,
    releaseKeyPassword,
)
val productionSigningText = releaseSigningValue("obdProductionSigning", "OBD_PRODUCTION_SIGNING")
val productionSigningRequested = when (productionSigningText) {
    null, "false" -> false
    "true" -> true
    else -> throw GradleException("OBD production signing must be exactly true or false.")
}

if (releaseSigningInputs.any { it != null } && releaseSigningInputs.any { it == null }) {
    throw GradleException(
        "Incomplete OBD release signing configuration. Provide all four keystore path, " +
            "keystore password, key alias and key password values, or remove all of them.",
    )
}
if (productionSigningRequested && releaseSigningInputs.any { it == null }) {
    throw GradleException(
        "Production OBD signing was requested but signing inputs are absent. " +
            "Set OBD_RELEASE_KEYSTORE_PATH, OBD_RELEASE_KEYSTORE_PASSWORD, " +
            "OBD_RELEASE_KEY_ALIAS and OBD_RELEASE_KEY_PASSWORD (or their Gradle properties).",
    )
}

val hasReleaseSigning = releaseSigningInputs.all { it != null }
val voltageOnlyAuditText = providers.gradleProperty("obdVoltageOnlyAudit").orNull ?: "false"
val voltageOnlyAudit = when (voltageOnlyAuditText) {
    "true" -> true
    "false" -> false
    else -> throw GradleException("obdVoltageOnlyAudit must be exactly true or false.")
}
val resolvedReleaseKeystore = releaseKeystorePath?.let(rootProject::file)
if (hasReleaseSigning && resolvedReleaseKeystore?.isFile != true) {
    throw GradleException("The configured OBD release keystore path is not a readable file.")
}

val buildGitSha = sequenceOf(
    providers.environmentVariable("GITHUB_SHA").orNull,
    runCatching {
        providers.exec {
            workingDir(rootProject.rootDir)
            commandLine("git", "rev-parse", "--verify", "HEAD")
            isIgnoreExitValue = true
        }.standardOutput.asText.get().trim()
    }.getOrNull(),
).mapNotNull { candidate ->
    candidate?.lowercase()?.takeIf { it.matches(Regex("[0-9a-f]{40,64}")) }
}.firstOrNull()?.take(12) ?: "unknown"

android {
    namespace = "com.dashcamstats.obdlogger"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.dashcamstats.obdlogger"
        minSdk = 26
        targetSdk = 34
        versionCode = 6
        versionName = "0.2.3"
        buildConfigField("String", "BUILD_GIT_SHA", "\"$buildGitSha\"")
        buildConfigField("boolean", "VOLTAGE_ONLY_AUDIT", voltageOnlyAudit.toString())
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("obdRelease") {
                storeFile = resolvedReleaseKeystore
                storePassword = releaseKeystorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            if (hasReleaseSigning) signingConfig = signingConfigs.getByName("obdRelease")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { buildConfig = true }
    testOptions { unitTests.isIncludeAndroidResources = true }
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    testImplementation("androidx.test:core-ktx:1.6.1")
    testImplementation("org.robolectric:robolectric:4.14.1")
}

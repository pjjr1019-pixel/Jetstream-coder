import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

val releaseVersion = Properties().apply {
    val file = rootProject.file("version.properties")
    if (file.exists()) file.inputStream().use { load(it) }
}

android {
    namespace = "com.deskhand.android"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.deskhand.android"
        minSdk = 28
        targetSdk = 36
        testInstrumentationRunner = "com.deskhand.android.HybridDecoderInstrumentation"
        versionCode = releaseVersion.getProperty("versionCode", "1").toInt()
        versionName = releaseVersion.getProperty("versionName", "0.1.0")
    }

    testOptions { unitTests.isIncludeAndroidResources = true }

    buildTypes {
        getByName("debug") {
            // A distinct package lets device verification coexist with the installed app.
            if (providers.gradleProperty("deskhandVerification").orNull == "true") {
                applicationIdSuffix = ".verification"
                versionNameSuffix = "-verification"
            }
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }
}

dependencies {
    implementation(files("libs/tsbridge.aar"))
    implementation(platform("androidx.compose:compose-bom:2026.09.00"))
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.16.1")
    testImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    testImplementation("org.json:json:20250517")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}

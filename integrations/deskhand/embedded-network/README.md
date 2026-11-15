# Embedded Android network bridge

Imported unchanged from the original Deskhand embedded-ts-prototype. `source-manifest.json` records source paths/hashes. Third-party modules are pinned in go.mod/go.sum; toolchains, caches and credentials are not checked in.

Build from the repository root using scripts/build-android-network.ps1 with ToolchainRoot (contains go/bin, bin/gomobile.exe and the cached gopath/pkg/mod) and AndroidSdk. Output replaces app/libs/tsbridge.aar only after the native verifier passes. The two supported ABIs remain arm64-v8a and x86_64. Compilation parallelism is bounded so local model inference remains usable. ELF LOAD segment alignment and final APK ZIP alignment are separately verified by scripts/verify-android-native.py.

Reference: https://developer.android.com/guide/practices/page-sizes

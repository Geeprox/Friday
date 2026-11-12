#!/usr/bin/env python3
"""Compile production Java and run pure wet/control lifecycle tests; no devices."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import os
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from ktv_build_tools import build_environment, isolated_command, toolchain_identity, workspace_receipt
from ktv_build_workspace import build_workspace


def main() -> None:
    root = ROOT
    jdk = root / ".artifacts/embodied-toolchain/jdk/bin"
    jar = root / ".artifacts/embodied-toolchain/android/android-35/android.jar"
    test_root = root / "native/KTVTV/tests"
    source_root = root / "native/KTVTV/android/src"
    compiler = shutil.which("c++")
    if compiler is None:
        raise RuntimeError("C++ compiler required for wet DSP and input-contract tests")
    with build_workspace(toolchain_identity(jdk.parent, compiler=compiler), "ktv-vocal-tests", configuration="ktv-system-audio") as (output, workspace):
        environment = build_environment(dict(os.environ), output)
        environment["JAVA_HOME"] = str(jdk.parent)
        environment["PATH"] = str(jdk) + os.pathsep + environment.get("PATH", "")

        def run(command, **options):
            return subprocess.run(isolated_command(environment, command), env=environment, **options)

        sources = sorted((test_root / "vocal_effects_fakes").rglob("*.java"))
        sources += [source_root / "local/friday/ktv" / (name + ".java") for name in (
            "VocalEffectsController", "VocalEffectsPolicy", "VocalEffectsIntent", "WetMixReservation")]
        sources += [test_root / "VocalEffectsTest.java",test_root / "VoiceProtectionSessionTest.java"]
        run([str(jdk / "javac"), "--release", "8", "-d", str(output),
                        *map(str, sources)], check=True)
        run([str(jdk / "java"), "-cp", str(output),
                        "local.friday.ktv.VocalEffectsTest"], check=True)
        run([str(jdk / "java"), "-cp", str(output), "local.friday.ktv.VoiceProtectionSessionTest"], check=True)
        # Exercise the real wet lease/cache lifecycle against pure resources.
        wet_tests = [test_root / (name + ".java") for name in ("WetPolicyTest", "WetCacheHookTest")]
        wet_sources = [source_root / "local/friday/audio" / (name + ".java")
                       for name in ("WetPolicy", "WetProfiles", "WetCacheHook")]
        run([str(jdk / "javac"), "--release", "8", "-d", str(output),
                        *map(str, wet_sources + wet_tests)], check=True)
        for test in wet_tests:
            run([str(jdk / "java"), "-cp", str(output),
                            "local.friday.audio." + test.stem], check=True)
        for name in ("WetDspTest", "WetInputContractTest", "FeedbackDetectorTest", "FeedbackClosedLoopTest", "VoiceMixBudgetTest", "InlineVoiceKernelTest", "InlineFeedbackClosedLoopTest"):
            executable = output / name
            run([compiler, "-std=c++17", "-O2", "-pthread",
                            str(test_root / (name + ".cpp")), "-o", str(executable)], check=True)
            run([str(executable)], check=True)
        production = output / "production"
        production.mkdir()
        tests = [test_root / (name + ".java") for name in (
            "MicrophoneIntentTest", "ImmediateControlTest", "LyricWindowTest",
            "QueueRefreshContinuityTest", "BackingStartupIntentTest", "MicMuteConfirmationTest", "CoordinatorClientTest",
            "CompatVocalEffectsTest", "PcmContractTest", "FeedbackNotchTest", "AudioBackendSelectionTest", "CompatMuteReadbackTest", "NativeVendorContractTest", "AudioEngineReleaseTest")]
        run([str(jdk / "javac"), "--release", "8", "-cp", str(jar),
                        "-d", str(production), *map(str, sorted(source_root.rglob("*.java")) + tests)], check=True)
        for test in tests:
            run([str(jdk / "java"), "-cp", str(production) + ":" + str(jar),
                            "local.friday.ktv." + test.stem, str(output)], check=True)

        guard = root / "native/KTVTV/audio-guard"
        guard_tests = sorted((guard / "tests").glob("*Test.java"))
        # Compile the real service and JNI adapter too; only pure test classes run.
        guard_sources = sorted((guard / "src").rglob("*.java"))
        run([str(jdk / "javac"), "--release", "8", "-cp", str(production) + ":" + str(jar),
                        "-d", str(production), *map(str, guard_sources + guard_tests)], check=True)
        for test in guard_tests:
            package = re.search(r"^package ([\w.]+);", test.read_text(), re.MULTILINE)
            name = (package.group(1) + "." if package else "") + test.stem
            run([str(jdk / "java"), "-cp", str(production) + ":" + str(jar), name], check=True)
    print(json.dumps({"offline_vocal_tests": "passed", "cpp_test_count": 7, "build_workspace": workspace_receipt(workspace)}))


if __name__ == "__main__":
    main()

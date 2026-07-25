from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dht_app.output_service_package import audit_output_service_package  # noqa: E402
from dht_app.output_service_paths import OutputServicePlan  # noqa: E402


MINIMUM_SELF_CONTAINED_EXE_BYTES = 10 * 1024 * 1024
FORBIDDEN_SIDECAR_SUFFIXES = (
    ".deps.json",
    ".runtimeconfig.json",
    ".dll",
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _verify_executable(path: Path, expected_name: str) -> None:
    _assert(path.is_file(), f"Missing self-contained executable: {path}")
    _assert(path.name == expected_name, f"Unexpected executable name: {path.name}")
    _assert(
        path.stat().st_size >= MINIMUM_SELF_CONTAINED_EXE_BYTES,
        f"Executable is too small to contain the .NET runtime: {path.stat().st_size}",
    )
    _assert(path.read_bytes()[:2] == b"MZ", f"Not a Windows PE executable: {path}")
    sibling_names = {item.name.lower() for item in path.parent.iterdir() if item.is_file()}
    for suffix in FORBIDDEN_SIDECAR_SUFFIXES:
        forbidden = path.with_suffix(suffix).name.lower()
        _assert(
            forbidden not in sibling_names,
            f"Framework-dependent sidecar must not be published: {forbidden}",
        )


def _run_without_global_runtime(
    executable: Path,
    arguments: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix="dht_no_dotnet_") as empty_root:
        environment["DOTNET_ROOT"] = empty_root
        environment["DOTNET_ROOT_X64"] = empty_root
        environment["DOTNET_MULTILEVEL_LOOKUP"] = "0"
        return subprocess.run(
            [str(executable), *arguments],
            cwd=str(executable.parent),
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )


def _make_output_plan(runtime_root: Path) -> OutputServicePlan:
    executable = runtime_root / "DualSenseOutputServer.exe"
    return OutputServicePlan(
        app_root=runtime_root.parent,
        source="package runtime folder",
        runtime_root=runtime_root,
        server_root=None,
        server_project=None,
        server_executable=executable if executable.is_file() else None,
        server_dll=None,
        start_script=None,
        stop_script=None,
        logs_root=runtime_root / "logs",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-server", type=Path, required=True)
    parser.add_argument("--sound-bridge", type=Path, required=True)
    args = parser.parse_args()

    output_server = args.output_server.resolve()
    sound_bridge = args.sound_bridge.resolve()
    _verify_executable(output_server, "DualSenseOutputServer.exe")
    _verify_executable(sound_bridge, "DualSenseSoundToHapticBridge.exe")
    package_audit = audit_output_service_package(
        _make_output_plan(output_server.parent)
    )
    _assert(package_audit.ok, package_audit.summary)
    _assert(
        package_audit.self_contained_executable,
        "App-side output package audit did not accept the self-contained EXE.",
    )

    with tempfile.TemporaryDirectory(prefix="dht_small_apphost_") as small_root:
        small_executable = Path(small_root) / "DualSenseOutputServer.exe"
        small_executable.write_bytes(b"MZ")
        small_audit = audit_output_service_package(
            _make_output_plan(Path(small_root))
        )
        _assert(
            not small_audit.ok and not small_audit.self_contained_executable,
            "App-side audit accepted a framework-dependent-size apphost.",
        )

    output_result = _run_without_global_runtime(
        output_server,
        ("--list-output-devices",),
    )
    _assert(
        output_result.returncode == 0,
        "Output server failed without a global .NET runtime: "
        + (output_result.stderr or output_result.stdout or "").strip(),
    )

    sound_result = _run_without_global_runtime(
        sound_bridge,
        ("--list-devices-json",),
    )
    _assert(
        sound_result.returncode == 0,
        "Sound bridge failed without a global .NET runtime: "
        + (sound_result.stderr or sound_result.stdout or "").strip(),
    )
    try:
        devices = json.loads(sound_result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise AssertionError(f"Sound bridge returned invalid JSON: {exc}") from exc
    _assert(isinstance(devices, list), "Sound bridge device result is not a list.")

    print("Self-contained .NET helper verification passed.")
    print(f"Output server bytes: {output_server.stat().st_size}")
    print(f"Sound bridge bytes: {sound_bridge.stat().st_size}")
    print("App-side package audit accepted only the self-contained output server.")
    print("Global .NET paths were replaced with an empty temporary directory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

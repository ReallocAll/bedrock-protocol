"""Build and execute generated native regressions with CMake, Ninja and nanobind 3.1.0.

python tests/compiler/native/run.py --work-dir <private-build-dir>
On Windows, run from a Visual Studio developer shell; pass
--cmake-arg=-DCMAKE_CXX_COMPILER=clang-cl to select clang-cl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    fixture = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=fixture.parents[2])
    parser.add_argument("--cmake-arg", action="append", default=[])
    args = parser.parse_args()
    source, work = args.source_root.resolve(), args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=str(source / "src"), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    commands = []

    def run(command, name, environment=env):
        command = list(map(str, command))
        log = work / f"{name}.log"
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=work, env=environment, stdout=output, stderr=subprocess.STDOUT)
        commands.append({"args": command, "cwd": str(work), "exit_code": result.returncode, "log": str(log)})
        (work / "commands.json").write_text(json.dumps(commands, indent=2) + "\n", encoding="utf-8")
        print(log.read_text(encoding="utf-8"), end="", flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)

    schema = work / "input/protocol"
    schema.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "protocol/__init__.py", schema / "__init__.py")
    shutil.copyfile(fixture / "schema.py", schema / "schema.py")
    generated = work / "generated"
    for language, output in (("cpp", generated / "include/bedrock/protocol"), ("nanobind", generated / "native")):
        run(
            [
                sys.executable,
                "-m",
                "bedrock_protocol",
                "--language",
                language,
                "--out",
                output,
                "--import-path",
                schema.parent,
                schema / "schema.py",
            ],
            f"generate-{language}",
        )
    (generated / "include/bedrock/protocol.hpp").write_text(
        "#pragma once\n#include <bedrock/protocol/schema.h>\n", encoding="utf-8"
    )
    package = work / "package/bedrock_protocol"
    package.mkdir(parents=True, exist_ok=True)
    for name in ("__init__.py", "_api.py"):
        shutil.copyfile(source / "src/bedrock_protocol" / name, package / name)
    for module in (generated / "native/bedrock_protocol").iterdir():
        shutil.copyfile(module, package / module.name)
    inputs = [
        *sorted((source / "src/bedrock_protocol").rglob("*.py")),
        *sorted((source / "src/python").glob("*.h")),
        source / "src/python/views.cpp",
        source / "src/python/nbt.cpp",
        *sorted((source / "include").rglob("*.hpp")),
        source / "protocol/__init__.py",
        *sorted(path for path in fixture.iterdir() if path.is_file()),
    ]
    (work / "inputs.json").write_text(
        json.dumps(
            {
                "python": sys.executable,
                "version": sys.version,
                "source": str(source),
                "sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    run(
        [
            "cmake",
            "-S",
            fixture,
            "-B",
            work / "build",
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DPython_EXECUTABLE={Path(sys.executable).as_posix()}",
            f"-DBPC_SOURCE_DIR={source.as_posix()}",
            f"-DFIXTURE_WORK_DIR={work.as_posix()}",
            *args.cmake_arg,
        ],
        "configure",
    )
    run(["cmake", "--build", work / "build", "--parallel", "2"], "build")
    native = next(package.glob("_native.*"))
    (work / "artifact.json").write_text(
        json.dumps(
            {
                "native": str(native),
                "sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    run(
        [sys.executable, "-m", "pytest", "-v", fixture / "check.py"],
        "checks",
        dict(env, PYTHONPATH=str(package.parent), BPC_NATIVE_FIXTURE_PACKAGE=str(package)),
    )


if __name__ == "__main__":
    main()

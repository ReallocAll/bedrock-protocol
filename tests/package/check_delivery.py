"""Stage tracked build input and check an independently installed wheel."""

import argparse
import ast
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path


def literal(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing {name}")


def stage(source, destination):
    assert not destination.exists(), destination
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=source).decode().split("\0")
    hashes = {}
    for name in filter(None, paths):
        path = Path(name)
        assert not any(part in {".local", ".archive", "archive", ".cache", "__pycache__"} for part in path.parts), name
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / path, target)
        hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip(),
        "files": hashes,
    }
    identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    manifest["input_sha256"] = identity
    destination.with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Tracked input: {manifest['commit']} / {identity}")


def extract(archive, destination):
    assert not destination.exists(), destination
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    assert len(roots) == 1 and roots[0].is_dir(), roots
    print(roots[0])


def declarations(tree, module):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            value = getattr(module, node.name)
            declarations(node, value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    getattr(module, target.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and not node.target.id.startswith("_")
        ):
            getattr(module, node.target.id)


def installed(schema):
    import bedrock_protocol as bp

    package = Path(bp.__file__).resolve().parent
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    assert (package / "py.typed").is_file()
    expected = literal(ast.parse(schema.read_text(encoding="utf-8")), "__supported_versions__")
    assert bp.SUPPORTED_VERSIONS == expected
    versions = [f"v{version}" for version in expected]
    assert [name for name in bp.__all__ if name.startswith("v") and name[1:].isdigit()] == versions
    stub = ast.parse((package / "__init__.pyi").read_text(encoding="utf-8"))
    assert bp.__all__ == literal(stub, "__all__")
    for name in bp.__all__:
        getattr(bp, name)
    for name in [*versions, "wire"]:
        module = importlib.import_module(f"bedrock_protocol.{name}")
        assert getattr(bp, name) is module
        stub = ast.parse((package / f"{name}.pyi").read_text(encoding="utf-8"))
        declarations(stub, module)
    print(f"Installed consumer: {package}; {len(expected)} versions, stubs and exports passed")


def compiler(schema, workspace):
    import bedrock_protocol as bp

    native = importlib.import_module("bedrock_protocol._native")
    package = Path(bp.__file__).resolve().parent
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    assert not workspace.is_relative_to(Path(__file__).resolve().parents[2]), workspace
    assert not workspace.exists(), workspace
    launcher = Path(sysconfig.get_path("scripts")) / ("bpc.exe" if os.name == "nt" else "bpc")
    assert launcher.is_file(), launcher
    native_path = Path(native.__file__).resolve()
    assert native_path.is_relative_to(package), native_path
    native_hash = hashlib.sha256(native_path.read_bytes()).hexdigest()
    inputs = workspace / "protocol"
    inputs.mkdir(parents=True)
    shutil.copyfile(schema, inputs / "__init__.py")
    (inputs / "installed.py").write_text(
        "from protocol import packet, uint32\n\n"
        "@packet(id=1)\nclass InstalledPacket:\n    value: uint32\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    subprocess.run(
        [str(launcher), "--language", "cpp", "--out", "generated", "--import-path", ".", "protocol/installed.py"],
        cwd=workspace,
        env=env,
        check=True,
    )
    header = workspace / "generated/installed.h"
    source = workspace / "generated/installed.cpp"
    assert "struct InstalledPacket" in header.read_text(encoding="utf-8")
    assert "Serializer<" in source.read_text(encoding="utf-8")
    assert hashlib.sha256(native_path.read_bytes()).hexdigest() == native_hash
    print(json.dumps({
        "launcher": str(launcher), "cwd": str(workspace), "package": str(package),
        "native": str(native_path), "native_sha256": native_hash,
        "outputs": [str(header), str(source)],
    }, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("stage", "extract"):
        command = commands.add_parser(name)
        command.add_argument("source", type=Path)
        command.add_argument("destination", type=Path)
    commands.add_parser("installed").add_argument("schema", type=Path)
    command = commands.add_parser("compiler")
    command.add_argument("schema", type=Path)
    command.add_argument("workspace", type=Path)
    args = parser.parse_args()
    if args.command == "installed":
        installed(args.schema)
    elif args.command == "compiler":
        compiler(args.schema.resolve(), args.workspace.resolve())
    elif args.command == "stage":
        stage(args.source.resolve(), args.destination.resolve())
    else:
        extract(args.source.resolve(), args.destination.resolve())

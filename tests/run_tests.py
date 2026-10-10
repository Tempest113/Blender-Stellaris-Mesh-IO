#!/usr/bin/env python3
"""Build the extension, install it into a throwaway Blender profile and run every test."""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEST_SCRIPT = REPO / "tests" / "blender_roundtrip_test.py"


def scrub(text):
    names = {getpass.getuser(), Path.home().name, os.environ.get("USERNAME", "")} - {""}
    for name in names:
        text = re.sub(re.escape(name), "USER", text, flags=re.IGNORECASE)
    return text


def run(cmd, env, cwd=None):
    print(scrub("$ " + " ".join(str(c) for c in cmd)), flush=True)
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        env=env,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    for line in proc.stdout:
        print(scrub(line), end="", flush=True)
    return proc.wait()


def bundled_python(blender):
    return next(iter(sorted(Path(blender).parent.glob("*/python/bin/python.exe"))), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--blender", default=os.environ.get("BLENDER_EXE"), help="Blender executable")
    parser.add_argument("--mesh", action="append", default=[], help="optional real .mesh file to round-trip (repeatable)")
    parser.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = parser.parse_args()
    if not args.blender:
        parser.error("pass --blender or set BLENDER_EXE")
    meshes = args.mesh or [m for m in os.environ.get("PDX_TEST_MESH", "").split(os.pathsep) if m]

    work = Path(tempfile.mkdtemp(prefix="pdx_blender_test_"))
    env = dict(os.environ, BLENDER_USER_RESOURCES=str(work / "profile"))
    dist, out, src = work / "dist", work / "out", work / "src"
    dist.mkdir()
    out.mkdir()
    shutil.copytree(REPO, src, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    try:
        for unit_tests in ("test_clausewitz_text.py", "test_pdx_asset.py", "test_asset_writer.py"):
            code = run([sys.executable, "-I", REPO / "tests" / unit_tests], env)
            if code:
                return code
        steps = [
            ["--command", "extension", "validate", src],
            ["--command", "extension", "build", "--source-dir", src, "--output-dir", dist],
        ]
        for step in steps:
            code = run([args.blender, *step], env)
            if code:
                return code

        zips = list(dist.glob("*.zip"))
        if len(zips) != 1:
            print(f"expected one built zip, found {len(zips)}")
            return 1
        zip_path = zips[0]

        code = run([args.blender, "--command", "extension", "install-file", "-r", "user_default", zip_path], env)
        if code:
            return code

        cmd = [args.blender, "-b", "--python", TEST_SCRIPT, "--", "--out", out]
        for mesh in meshes:
            cmd += ["--mesh", mesh]
        code = run(cmd, env)

        mesh_file, json_file = out / "synthetic.mesh", out / "cli.json"
        python = bundled_python(args.blender)
        if mesh_file.is_file() and python:
            cli = [python, "-m", "src", "convert_to", "-i", mesh_file]
            cli_code = run(cli + ["-o", json_file, "-f", "json"], env, cwd=work) or run(cli, env, cwd=work)
            valid = cli_code == 0 and bool(json.loads(json_file.read_text(encoding="utf-8")))
            print(f"[{'PASS' if valid else 'FAIL'}] command line: convert_to json and print")
            code = code or (0 if valid else 1)
        return code
    finally:
        if args.keep:
            print(scrub(f"kept: {work}"))
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

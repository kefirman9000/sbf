"""Copies this app's farmcore.py (the code shared by CBBF, OBF and SBF) to the other apps and records its fingerprint.

    python tools/sync_core.py ../cbbf ../obf ../sbf

Run it from the app whose farmcore.py you changed. Each app's tests/test_core.py checks that its copy matches the
fingerprint, so a change made to only one app's copy shows up as a failing test instead of quietly drifting apart.
"""
import hashlib
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fingerprint(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def main(targets):
    src = os.path.join(HERE, "farmcore.py")
    fp = fingerprint(src)
    for app in [HERE] + [os.path.abspath(t) for t in targets]:
        if not os.path.exists(os.path.join(app, "engine.py")):
            print(f"skipped {app}: not an app folder")
            continue
        if app != HERE:
            shutil.copyfile(src, os.path.join(app, "farmcore.py"))
        os.makedirs(os.path.join(app, "tests"), exist_ok=True)
        with open(os.path.join(app, "tests", "farmcore.sha256"), "w") as f:
            f.write(fp + "\n")
        print(f"{app}: farmcore.py {fp[:12]}")


if __name__ == "__main__":
    main(sys.argv[1:])

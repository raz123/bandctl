#!/usr/bin/env python3
"""Release-zip gate for the Band Controller module.

Fails a release zip that cannot boot on a stock ROM or that ships an
untested protocol ABI. Run before publishing any bandctl-*.zip:

  * python/bin/python3.14 must exist — the interpreter service.sh selects
    (A-152/A-177: v2.5 shipped 21 members with no python/bin or python/lib
    and aborted on any ROM without Termux/pyroot).
  * the zipped diag/protocol.py must be byte-identical to the tested repo
    copy (A-151: a stale mirror masked the shipped A-01 TypeError).
  * every bundled _ssl/_hashlib module's DT_NEEDED closure must be
    satisfiable on-device: libssl.so.3 / libcrypto.so.3 must ship with the
    bundle, because bionic's unversioned BoringSSL libs cannot substitute
    the Termux SONAMEs (A-212). Shipping neither the libs nor the modules
    (stripped) also passes.

Usage: python3 tools/check_release_zip.py bandctl-vX.Y.zip
"""
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO_PROTOCOL = (ROOT / "diag" / "protocol.py").read_bytes()

DYNLOAD_PREFIX = "python/lib/python3.14/lib-dynload/"
# (module suffix, required bundle lib) — libs must be present iff the
# module ships, because bionic cannot satisfy the Termux SONAMEs.
NEEDED_LIBS = {
    "_ssl": "libssl.so.3",
    "_hashlib": "libcrypto.so.3",
}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_release_zip.py <module.zip>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    errors = []
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        print(f"FAIL: {path}: not a readable zip: {e}", file=sys.stderr)
        return 1
    with zf:
        names = set(zf.namelist())

        if "python/bin/python3.14" not in names:
            errors.append(
                "missing python/bin/python3.14 (bundled interpreter; "
                "service.sh cannot start — A-152/A-177)"
            )

        try:
            zipped = zf.read("diag/protocol.py")
        except KeyError:
            errors.append("missing diag/protocol.py in zip")
        else:
            if zipped != REPO_PROTOCOL:
                errors.append(
                    "diag/protocol.py in zip differs from the tested repo "
                    "copy (A-151 ABI-drift gate)"
                )

        dynload = {n for n in names if n.startswith(DYNLOAD_PREFIX)}
        bundle_libs = {
            n.rsplit("/", 1)[-1]
            for n in names
            if n.startswith("python/usr/lib/") and ".so" in n
        }
        for module, required_lib in NEEDED_LIBS.items():
            shipped = [n for n in dynload if module in n]
            if shipped and required_lib not in bundle_libs:
                errors.append(
                    f"{','.join(sorted(shipped))} ships but {required_lib} "
                    f"is absent — DT_NEEDED unsatisfiable on bionic (A-212); "
                    "bundle the lib or strip the module"
                )

    if errors:
        for e in errors:
            print(f"FAIL: {path}: {e}", file=sys.stderr)
        return 1
    print(f"OK: {path} — interpreter, protocol ABI, and bundle closure verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Make a Python 3.9-compatible copy of an aiokwikset wheel.

aiokwikset declares Python >= 3.10, but its code only needs 3.10 for the
"X | None" annotation syntax. Most modules already defer annotations with
"from __future__ import annotations"; this adds that line to the ones that
don't (0.7.3: errors.py, user.py), sets Requires-Python to >=3.9, tags the
version "+py39" and rewrites RECORD so pip accepts the wheel.

    python3 make_py39_wheel.py aiokwikset-0.7.3-py3-none-any.whl
      -> aiokwikset-0.7.3+py39-py3-none-any.whl

aiokwikset is Apache-2.0 (c) Brad Barbour; the modified files carry a notice.
"""
import base64
import hashlib
import re
import sys
import zipfile
from pathlib import Path

FUTURE = "from __future__ import annotations"
NOTICE = "# Modified by Savant HaloLink: deferred annotations for Python 3.9 (no functional change).\n"


def add_future(src: str) -> str:
    if re.search(r"^from __future__ import annotations", src, re.M):
        return src
    # after the module docstring, if there is one
    m = re.match(r'(\s*(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')\s*\n)', src)
    cut = m.end() if m else 0
    return src[:cut] + "\n" + NOTICE + FUTURE + "\n" + src[cut:]


def record_line(name, data):
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def main(path):
    src = Path(path)
    m = re.match(r"aiokwikset-([^-]+)-(.+\.whl)$", src.name)
    if not m:
        raise SystemExit("expected an aiokwikset-<version>-...whl file")
    version = m.group(1).split("+")[0]
    new_version = version + "+py39"
    out = src.with_name(f"aiokwikset-{new_version}-{m.group(2)}")
    old_info, new_info = f"aiokwikset-{version}.dist-info", f"aiokwikset-{new_version}.dist-info"
    files, patched = {}, []
    with zipfile.ZipFile(src) as z:
        for n in z.namelist():
            files[n] = z.read(n)
    result = {}
    for name, data in files.items():
        new_name = name.replace(old_info + "/", new_info + "/")
        if name.endswith("/RECORD"):
            continue
        if name.startswith("aiokwikset/") and name.endswith(".py"):
            text = data.decode("utf-8")
            fixed = add_future(text)
            if fixed != text:
                patched.append(name)
                data = fixed.encode("utf-8")
        if name.endswith("/METADATA"):
            text = data.decode("utf-8")
            text = re.sub(r"^Version: .*$", f"Version: {new_version}", text, count=1, flags=re.M)
            text = re.sub(r"^Requires-Python: .*$", "Requires-Python: >=3.9", text, count=1, flags=re.M)
            data = text.encode("utf-8")
        result[new_name] = data
    record_name = f"{new_info}/RECORD"
    lines = [record_line(n, d) for n, d in result.items()] + [f"{record_name},,"]
    result[record_name] = ("\n".join(lines) + "\n").encode()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in result.items():
            z.writestr(n, d)
    print(f"wrote {out.name}; added deferred annotations to: {', '.join(patched) or 'nothing (already 3.9-ready)'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])

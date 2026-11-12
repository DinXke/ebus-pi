"""Releasepakket maken: dist/ebus-pi-<versie>.tar.gz + .sha256 (voor GitHub-release of Flux).

  python tools/build_release.py
Upload beide bestanden als assets van release 'v<versie>'; de Pi vindt ze via Updates.
"""
import hashlib
import io
import os
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ebuspi import VERSION  # noqa: E402


def main():
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", f"ebus-pi-{VERSION}.tar.gz")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for top in ("ebuspi", "system", "README.md", "install.sh"):
            def flt(ti):
                return None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti
            tar.add(os.path.join(ROOT, top), arcname=top, filter=flt)
    data = buf.getvalue()
    with open(out, "wb") as f:
        f.write(data)
    sha = hashlib.sha256(data).hexdigest()
    with open(out + ".sha256", "w", newline="\n") as f:
        f.write(f"{sha}  {os.path.basename(out)}\n")
    print(out, sha)


if __name__ == "__main__":
    main()

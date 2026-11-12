"""Demo die blijft draaien en zichzelf herstart bij elke codewijziging (ebuspi/*.py).

  python tools/demo_reload.py [--port 8080] [--live HOST]
De beheerpagina (static/admin.html) wordt per verzoek gelezen: vernieuwen in de browser volstaat.
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "ebuspi")


def stamp():
    t = 0.0
    for dp, _, files in os.walk(PKG):
        for f in files:
            if f.endswith(".py"):
                try:
                    t = max(t, os.path.getmtime(os.path.join(dp, f)))
                except OSError:
                    pass
    return t


def main():
    args = sys.argv[1:]
    while True:
        s = stamp()
        p = subprocess.Popen([sys.executable, "-m", "ebuspi.demo", *args], cwd=ROOT)
        try:
            while p.poll() is None and stamp() == s:
                time.sleep(1)
        except KeyboardInterrupt:
            p.terminate()
            return
        if p.poll() is None:
            print("[demo] code gewijzigd: herstarten", flush=True)
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        else:
            print(f"[demo] gestopt (code {p.returncode}); opnieuw over 3 s", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()

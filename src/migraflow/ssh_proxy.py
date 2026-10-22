"""Standalone rsync -e transport: prefix SSH stdin with a JSON root/lease header."""
import json
import os
import subprocess
import sys
import threading

from ssh_protocol import ssh_arguments


def main():
    with open(sys.argv[1], encoding="utf-8") as file:
        header = json.load(file)
    if len(sys.argv) < 4 or sys.argv[2] != header["host"]:
        raise ValueError("Unexpected SSH transport host.")
    process = subprocess.Popen(ssh_arguments(header["host"]) + sys.argv[3:], stdin=subprocess.PIPE,
                               stdout=sys.stdout.buffer, stderr=sys.stderr.buffer)
    try:
        process.stdin.write(json.dumps(header, ensure_ascii=True).encode("ascii") + b"\n")
        process.stdin.flush()

        def forward():
            try:
                while True:
                    chunk = os.read(0, 65536)
                    if not chunk:
                        break
                    process.stdin.write(chunk)
                    process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        threading.Thread(target=forward, daemon=True).start()
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


if __name__ == "__main__":
    raise SystemExit(main())

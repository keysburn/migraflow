"""Standalone rsync supervisor; also runs inside WSL without third-party packages."""
import argparse
import os
import signal
import subprocess
import sys
import threading
from contextlib import ExitStack, nullcontext
from pathlib import Path

from locking import file_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--forward-lease", action="store_true")
    parser.add_argument("--pin-roots", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        with (file_lock(Path(args.lock)) if args.lock else nullcontext()), ExitStack() as roots:
            descriptors = []
            if args.pin_roots:
                for index in (-2, -1):
                    fd = os.open(command[index], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    roots.callback(os.close, fd)
                    descriptors.append(fd)
                    command[index] = f"/proc/self/fd/{fd}/"
            print("READY", flush=True)
            if os.read(sys.stdin.fileno(), 1) != b"1":
                return 20
            with Path(args.manifest).open("rb") as manifest:
                process = subprocess.Popen(command, stdin=subprocess.PIPE if args.forward_lease else manifest,
                                           stdout=subprocess.PIPE if args.forward_lease else sys.stderr,
                                           stderr=sys.stderr, start_new_session=os.name != "nt",
                                           pass_fds=tuple(descriptors))
                stopped = threading.Event()
                stop_lock = threading.Lock()

                def stop():
                    with stop_lock:
                        if process.poll() is not None:
                            return
                        if args.forward_lease:
                            process.stdin.close()  # WSL guardian sees EOF and kills its rsync group.
                        else:
                            try:
                                os.killpg(process.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            if args.forward_lease:
                                # WSL must acknowledge Linux child cleanup before releasing worker.lock.
                                process.wait()
                            elif os.name == "nt":
                                process.kill()
                            else:
                                os.killpg(process.pid, signal.SIGKILL)
                            process.wait()

                def watch_parent():
                    while os.read(sys.stdin.fileno(), 4096):
                        pass  # EOF includes abrupt coordinator death; avoid buffered IO locks at shutdown.
                    stopped.set()
                    stop()

                threading.Thread(target=watch_parent, daemon=True).start()
                if args.forward_lease:
                    if process.stdout.readline() != b"READY\n" or stopped.is_set():
                        stop()
                        return 20
                    try:
                        process.stdin.write(b"1")
                        process.stdin.flush()
                    except BrokenPipeError:
                        stop()
                        return 20
                try:
                    code = process.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    stop()
                    return 30
                return 20 if stopped.is_set() else (code if code >= 0 else 20)
    except OSError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 11


if __name__ == "__main__":
    raise SystemExit(main())

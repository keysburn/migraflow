"""Fixed SSH commands shared with the standalone rsync transport proxy."""
import base64
import re
from pathlib import Path


def ssh_arguments(host):
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", host):
        raise ValueError("Invalid OpenSSH host alias.")
    return ["ssh", "-T", "-a", "-x", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "ClearAllForwardings=yes", "-o", "RemoteCommand=none", "-o", "RequestTTY=no",
            "-o", "ConnectTimeout=15", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", host]


def helper_command(mode):
    encoded = base64.b64encode(Path(__file__).with_name("ssh_worker.py").read_bytes()).decode("ascii")
    # Only fixed package code is placed in the remote command, never a user path/option.
    return "python3 -I -B -u -X utf8 -c \"import base64;exec(base64.b64decode('" + encoded + "'))\" " + mode

#!/usr/bin/env python3
"""Run a command inside a fresh network namespace with ONLY loopback up (no route to anything else).

usage: unshare -n python3 -I netns_exec.py -- <cmd> [args...]
(We run as root in the sandbox; `unshare -n` works there.  Loopback is needed for Marionette, which listens on 127.0.0.1.)
"""
import fcntl
import os
import socket
import struct
import sys

SIOCGIFFLAGS, SIOCSIFFLAGS, IFF_UP = 0x8913, 0x8914, 0x1
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ifr = struct.pack("16sH14s", b"lo", 0, b"\0" * 14)
flags = struct.unpack("16sH14s", fcntl.ioctl(s, SIOCGIFFLAGS, ifr))[1]
fcntl.ioctl(s, SIOCSIFFLAGS, struct.pack("16sH14s", b"lo", flags | IFF_UP, b"\0" * 14))
s.close()
cmd = sys.argv[sys.argv.index("--") + 1:]
os.execvp(cmd[0], cmd)

#!/usr/local/bin/python
import subprocess
import sys

try:
    subprocess.check_call(
        ["wget", "--quiet", "--tries=1", "--spider", "http://localhost:8008/health"]
    )
except subprocess.CalledProcessError:
    sys.exit(1)

#!/usr/local/bin/python
import subprocess
import sys

try:
    subprocess.check_call(["curl", "-fSs", "http://localhost:8008/health"])
except subprocess.CalledProcessError:
    sys.exit(1)

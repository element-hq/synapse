#!/bin/sh
set -eu

# On 32-bit Linux platforms, we need libatomic1 to use rustup
if command -v apt-get &> /dev/null; then
    apt-get update
    apt-get install libatomic1
fi

# Install a Rust toolchain
curl https://sh.rustup.rs -sSf | sh -s -- --default-toolchain 1.82.0 -y --profile minimal

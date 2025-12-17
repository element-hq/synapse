#!/bin/sh
set -xeu

# On 32-bit Linux platforms, we need libatomic1 to use rustup
if command -v yum &> /dev/null; then
   yum install -y libatomic
fi

# Install a Rust toolchain
curl https://sh.rustup.rs -sSf | sh -s -- --default-toolchain stable -y --profile minimal

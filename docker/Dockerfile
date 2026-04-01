# syntax=docker/dockerfile:1
# Dockerfile to build the matrixdotorg/synapse docker images.
#
# Note that it uses features which are only available in BuildKit - see
# https://docs.docker.com/go/buildkit/ for more information.
#
# To build the image, run `docker build` command from the root of the
# synapse repository:
#
#    DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile .
#
# There is an optional PYTHON_VERSION build argument which sets the
# version of python to build against: for example:
#
#    DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile --build-arg PYTHON_VERSION=3.10 .
#

ARG DEBIAN_VERSION=trixie
ARG PYTHON_VERSION=3.13

###
### Stage 0: builder
###
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-${DEBIAN_VERSION} AS builder

# This silences a warning as uv isn't able to do hardlinks between its cache
# (mounted as --mount=type=cache) and the target directory.
ENV UV_LINK_MODE=copy

# Install rust and ensure its in the PATH
ENV RUSTUP_HOME=/rust
ENV CARGO_HOME=/cargo
ENV PATH=/cargo/bin:/rust/bin:$PATH
RUN mkdir /rust /cargo

RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path --default-toolchain stable --profile minimal

# arm64 builds consume a lot of memory if `CARGO_NET_GIT_FETCH_WITH_CLI` is not
# set to true, so we expose it as a build-arg.
ARG CARGO_NET_GIT_FETCH_WITH_CLI=false
ENV CARGO_NET_GIT_FETCH_WITH_CLI=$CARGO_NET_GIT_FETCH_WITH_CLI

WORKDIR /synapse

# If specified, we won't verify the hashes of dependencies.
# This is only needed if the hashes of dependencies cannot be checked for some
# reason, such as when a git repository is used directly as a dependency.
ARG TEST_ONLY_SKIP_DEP_HASH_VERIFICATION

# If specified, we won't use the uv lockfile.
# Instead, we'll just install what a regular `pip install` would from PyPI.
ARG TEST_ONLY_IGNORE_LOCKFILE

# First, install only the dependencies (without the project itself).
# This layer is cached as long as pyproject.toml and uv.lock don't change,
# so dependency bytecode compilation (~4000 files) only happens once.
COPY pyproject.toml uv.lock /synapse/
RUN --mount=type=cache,target=/root/.cache/uv \
  if [ -z "$TEST_ONLY_IGNORE_LOCKFILE" ]; then \
    uv sync --extra all --frozen --no-install-project --no-dev \
      ${TEST_ONLY_SKIP_DEP_HASH_VERIFICATION:+--no-verify-hashes} \
      --compile-bytecode; \
  fi

# Now copy the source and install the project itself.
# On a Python-only change, only the synapse package is reinstalled and
# only its bytecode needs recompiling.
COPY synapse /synapse/synapse/
COPY rust /synapse/rust/
COPY README.rst build_rust.py Cargo.toml Cargo.lock /synapse/

RUN \
  --mount=type=cache,target=/root/.cache/uv \
  --mount=type=cache,target=/synapse/target,sharing=locked \
  --mount=type=cache,target=${CARGO_HOME}/registry,sharing=locked \
  if [ -z "$TEST_ONLY_IGNORE_LOCKFILE" ]; then \
    uv sync --extra all --frozen --no-dev --no-editable \
      ${TEST_ONLY_SKIP_DEP_HASH_VERIFICATION:+--no-verify-hashes} \
      --compile-bytecode; \
  else \
    uv pip install --python=.venv /synapse[all] --compile-bytecode; \
  fi

###
### Stage 1: runtime dependencies download for ARM64 and AMD64
###
FROM --platform=$BUILDPLATFORM docker.io/library/debian:${DEBIAN_VERSION} AS runtime-deps

# Tell apt to keep downloaded package files, as we're using cache mounts.
RUN rm -f /etc/apt/apt.conf.d/docker-clean; echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

# Add both target architectures
RUN dpkg --add-architecture arm64
RUN dpkg --add-architecture amd64

# Fetch the runtime dependencies debs for both architectures
# We do that by building a recursive list of packages we need to download with `apt-cache depends`
# and then downloading them with `apt-get download`.
RUN \
  --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt,sharing=locked \
  apt-get update -qq && \
  apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances --no-pre-depends \
      curl \
      gosu \
      libjpeg62-turbo \
      libpq5 \
      libwebp7 \
      xmlsec1 \
      libjemalloc2 \
    | grep '^\w' > /tmp/pkg-list && \
  for arch in arm64 amd64; do \
    mkdir -p /tmp/debs-${arch} && \
    chown _apt:root /tmp/debs-${arch} && \
    cd /tmp/debs-${arch} && \
    apt-get -o APT::Architecture="${arch}" download $(cat /tmp/pkg-list); \
  done

# Extract the debs for each architecture
RUN \
  for arch in arm64 amd64; do \
    mkdir -p /install-${arch}/var/lib/dpkg/status.d/ && \
    for deb in /tmp/debs-${arch}/*.deb; do \
      package_name=$(dpkg-deb -I ${deb} | awk '/^ Package: .*$/ {print $2}'); \
      echo "Extracting: ${package_name}"; \
      dpkg --ctrl-tarfile $deb | tar -Ox ./control > /install-${arch}/var/lib/dpkg/status.d/${package_name}; \
      dpkg --extract $deb /install-${arch}; \
    done; \
  done


###
### Stage 2: runtime
###

FROM docker.io/library/python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS runtime

ARG TARGETARCH

# If specified, Synapse will use this as the version string in the app.
#
# This can be useful to capture the git info of the build as `.git/` won't be
# available in the Docker image for Synapse to generate from.
ARG SYNAPSE_VERSION_STRING
# Pass it through to Synapse as an environment variable.
ENV SYNAPSE_VERSION_STRING=${SYNAPSE_VERSION_STRING}

LABEL org.opencontainers.image.url='https://github.com/element-hq/synapse'
LABEL org.opencontainers.image.documentation='https://element-hq.github.io/synapse/latest/'
LABEL org.opencontainers.image.source='https://github.com/element-hq/synapse.git'
LABEL org.opencontainers.image.licenses='AGPL-3.0-or-later OR LicenseRef-Element-Commercial'

COPY --from=runtime-deps /install-${TARGETARCH}/etc /etc
COPY --from=runtime-deps /install-${TARGETARCH}/usr /usr
COPY --from=runtime-deps /install-${TARGETARCH}/var /var

# Copy the virtual environment from the builder stage.
COPY --from=builder /synapse/.venv /synapse/.venv

# Put the virtualenv on PATH
ENV PATH="/synapse/.venv/bin:$PATH"

COPY ./docker/start.py /start.py
COPY ./docker/conf /conf

# 8008: CS Matrix API port from Synapse
# 8448: SS Matrix API port from Synapse
EXPOSE 8008/tcp 8448/tcp
# 19090: Metrics listener port for the main process (metrics must be enabled with
# SYNAPSE_ENABLE_METRICS=1).
EXPOSE 19090/tcp

ENTRYPOINT ["/start.py"]

HEALTHCHECK --start-period=5s --interval=15s --timeout=5s \
  CMD curl -fSs http://localhost:8008/health || exit 1


###
### Stage 3: simplified runtime for complement/local testing (single-arch)
###
### This stage skips the multi-arch runtime-deps dance and just installs
### runtime dependencies via apt for the host architecture only.
###

FROM docker.io/library/python:${PYTHON_VERSION}-slim-${DEBIAN_VERSION} AS complement-runtime

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update -qq && \
    apt-get install -yqq --no-install-recommends \
        curl gosu libjpeg62-turbo libpq5 libwebp7 xmlsec1 libjemalloc2

# Copy the virtual environment from the builder stage.
COPY --from=builder /synapse/.venv /synapse/.venv

# Put the virtualenv on PATH
ENV PATH="/synapse/.venv/bin:$PATH"

ARG SYNAPSE_VERSION_STRING
ENV SYNAPSE_VERSION_STRING=${SYNAPSE_VERSION_STRING}

LABEL org.opencontainers.image.url='https://github.com/element-hq/synapse'
LABEL org.opencontainers.image.documentation='https://element-hq.github.io/synapse/latest/'
LABEL org.opencontainers.image.source='https://github.com/element-hq/synapse.git'
LABEL org.opencontainers.image.licenses='AGPL-3.0-or-later OR LicenseRef-Element-Commercial'

COPY ./docker/start.py /start.py
COPY ./docker/conf /conf

EXPOSE 8008/tcp 8448/tcp
EXPOSE 19090/tcp

ENTRYPOINT ["/start.py"]

HEALTHCHECK --start-period=5s --interval=15s --timeout=5s \
    CMD curl -fSs http://localhost:8008/health || exit 1

// Docker Bake configuration for building Synapse images.
//
// Usage:
//   docker buildx bake              # Build the base synapse image (production)
//   docker buildx bake complement   # Build the full complement test image chain
//
// Variables can be overridden via environment variables with the same name, e.g.:
//   PYTHON_VERSION=3.12 docker buildx bake complement

variable "LOCAL_IMAGE_NAMESPACE" {
  default = "localhost"
}

variable "PYTHON_VERSION" {
  default = "3.13"
}

variable "DEBIAN_VERSION" {
  default = "trixie"
}

variable "SYNAPSE_VERSION_STRING" {
  default = ""
}

// Used by twisted_trunk.yml CI to skip hash verification for git dependencies.
variable "TEST_ONLY_SKIP_DEP_HASH_VERIFICATION" {
  default = ""
}

// Used by latest_deps.yml CI to ignore lockfile and resolve freely from PyPI.
variable "TEST_ONLY_IGNORE_LOCKFILE" {
  default = ""
}

// ---- Shared base configuration ----

target "_synapse_common" {
  context    = "."
  dockerfile = "docker/Dockerfile"
  args = {
    PYTHON_VERSION                       = PYTHON_VERSION
    DEBIAN_VERSION                       = DEBIAN_VERSION
    SYNAPSE_VERSION_STRING               = SYNAPSE_VERSION_STRING
    TEST_ONLY_SKIP_DEP_HASH_VERIFICATION = TEST_ONLY_SKIP_DEP_HASH_VERIFICATION
    TEST_ONLY_IGNORE_LOCKFILE            = TEST_ONLY_IGNORE_LOCKFILE
  }
}

// ---- Production targets ----

group "default" {
  targets = ["synapse"]
}

target "synapse" {
  inherits = ["_synapse_common"]
  tags     = ["${LOCAL_IMAGE_NAMESPACE}/synapse:latest"]
  // Defaults to the last stage ("complement-runtime"), so we explicitly
  // target the "runtime" stage for production builds.
  target = "runtime"
}

target "synapse-workers" {
  context    = "."
  dockerfile = "docker/Dockerfile-workers"
  tags       = ["${LOCAL_IMAGE_NAMESPACE}/synapse-workers:latest"]
  contexts = {
    "${LOCAL_IMAGE_NAMESPACE}/synapse" = "target:synapse"
  }
  args = {
    FROM           = "${LOCAL_IMAGE_NAMESPACE}/synapse"
    PYTHON_VERSION = PYTHON_VERSION
    DEBIAN_VERSION = DEBIAN_VERSION
  }
}

// ---- Complement targets ----
//
// These use the "complement-runtime" stage which skips the multi-arch
// runtime-deps download and just apt-get installs for the host arch.

group "complement" {
  targets = ["complement-synapse"]
}

target "_synapse_complement_base" {
  inherits = ["_synapse_common"]
  target   = "complement-runtime"
  tags     = ["${LOCAL_IMAGE_NAMESPACE}/synapse:latest"]
}

target "_synapse_workers_complement" {
  context    = "."
  dockerfile = "docker/Dockerfile-workers"
  tags       = ["${LOCAL_IMAGE_NAMESPACE}/synapse-workers:latest"]
  contexts = {
    "${LOCAL_IMAGE_NAMESPACE}/synapse" = "target:_synapse_complement_base"
  }
  args = {
    FROM           = "${LOCAL_IMAGE_NAMESPACE}/synapse"
    PYTHON_VERSION = PYTHON_VERSION
    DEBIAN_VERSION = DEBIAN_VERSION
  }
}

target "complement-synapse" {
  context    = "docker/complement"
  dockerfile = "Dockerfile"
  tags       = ["${LOCAL_IMAGE_NAMESPACE}/complement-synapse:latest"]
  contexts = {
    "${LOCAL_IMAGE_NAMESPACE}/synapse-workers" = "target:_synapse_workers_complement"
  }
  args = {
    FROM           = "${LOCAL_IMAGE_NAMESPACE}/synapse-workers"
    DEBIAN_VERSION = DEBIAN_VERSION
  }
}

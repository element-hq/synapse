#!/bin/bash
#
# Default ENTRYPOINT for the docker image used for testing synapse with workers under complement

set -e

echo "Complement Synapse launcher"
echo "  Args: $*"
echo "  Env: SYNAPSE_COMPLEMENT_DATABASE=$SYNAPSE_COMPLEMENT_DATABASE SYNAPSE_COMPLEMENT_USE_WORKERS=$SYNAPSE_COMPLEMENT_USE_WORKERS SYNAPSE_COMPLEMENT_USE_ASYNCIO_REACTOR=$SYNAPSE_COMPLEMENT_USE_ASYNCIO_REACTOR SYNAPSE_COMPLEMENT_USE_MAS=$SYNAPSE_COMPLEMENT_USE_MAS"

function log {
    d=$(printf '%(%Y-%m-%d %H:%M:%S)T,%.3s\n' ${EPOCHREALTIME/./ })
    echo "$d $*"
}

# Set the server name of the homeserver
export SYNAPSE_SERVER_NAME=${SERVER_NAME}

# No need to report stats here
export SYNAPSE_REPORT_STATS=no


case "$SYNAPSE_COMPLEMENT_DATABASE" in
  postgres)
    # Set postgres authentication details which will be placed in the homeserver config file
    export POSTGRES_PASSWORD=somesecret
    export POSTGRES_USER=postgres
    export POSTGRES_HOST=localhost

    # configure supervisord to start postgres
    export START_POSTGRES=true
    ;;

  sqlite|"")
    # Set START_POSTGRES to false unless it has already been set
    # (i.e. by another container image inheriting our own).
    export START_POSTGRES=${START_POSTGRES:-false}
    ;;

  *)
    echo "Unknown Synapse database: SYNAPSE_COMPLEMENT_DATABASE=$SYNAPSE_COMPLEMENT_DATABASE" >&2
    exit 1
    ;;
esac


if [[ -n "$SYNAPSE_COMPLEMENT_USE_WORKERS" ]]; then
  # Specify the workers to test with
  # Allow overriding by explicitly setting SYNAPSE_WORKER_TYPES outside, while still
  # utilizing WORKERS=1 for backwards compatibility.
  # -n True if the length of string is non-zero.
  # -z True if the length of string is zero.
  if [[ -z "$SYNAPSE_WORKER_TYPES" ]]; then
    export SYNAPSE_WORKER_TYPES="\
      event_persister:2, \
      background_worker, \
      event_creator, \
      user_dir, \
      media_repository, \
      federation_inbound, \
      federation_reader, \
      federation_sender, \
      synchrotron, \
      client_reader, \
      appservice, \
      pusher, \
      device_lists:2, \
      stream_writers=account_data+presence+receipts+to_device+typing"

  fi
  log "Workers requested: $SYNAPSE_WORKER_TYPES"
  # adjust connection pool limits on worker mode as otherwise running lots of worker synapses
  # can make docker unhappy (in GHA)
  export POSTGRES_CP_MIN=1
  export POSTGRES_CP_MAX=3
  echo "using reduced connection pool limits for worker mode"
  # Improve startup times by using a launcher based on fork()
  export SYNAPSE_USE_EXPERIMENTAL_FORKING_LAUNCHER=1
else
  # Empty string here means 'main process only'
  export SYNAPSE_WORKER_TYPES=""
fi


if [[ -n "$SYNAPSE_COMPLEMENT_USE_ASYNCIO_REACTOR" ]]; then
  if [[ -n "$SYNAPSE_USE_EXPERIMENTAL_FORKING_LAUNCHER" ]]; then
    export SYNAPSE_COMPLEMENT_FORKING_LAUNCHER_ASYNC_IO_REACTOR="1"
  else
    export SYNAPSE_ASYNC_IO_REACTOR="1"
  fi
else
  export SYNAPSE_ASYNC_IO_REACTOR="0"
fi


# Add Complement's appservice registration directory, if there is one
# (It can be absent when there are no application services in this test!)
if [ -d /complement/appservice ]; then
    export SYNAPSE_AS_REGISTRATION_DIR=/complement/appservice
fi

# Generate a TLS key, then generate a certificate by having Complement's CA sign it
# Note that both the key and certificate are in PEM format (not DER).

# First generate a configuration file to set up a Subject Alternative Name.
echo "\
.include /etc/ssl/openssl.cnf

[SAN]
subjectAltName=DNS:${SERVER_NAME}" > /conf/server.tls.conf

# Generate an RSA key
openssl genrsa -out /conf/server.tls.key 2048

# Generate a certificate signing request
openssl req -new -config /conf/server.tls.conf -key /conf/server.tls.key -out /conf/server.tls.csr \
  -subj "/CN=${SERVER_NAME}" -reqexts SAN

# Make the Complement Certificate Authority sign and generate a certificate.
openssl x509 -req -in /conf/server.tls.csr \
  -CA /complement/ca/ca.crt -CAkey /complement/ca/ca.key -set_serial 1 \
  -out /conf/server.tls.crt -extfile /conf/server.tls.conf -extensions SAN

# Assert that we have a Subject Alternative Name in the certificate.
# (the test will exit with 1 here if there isn't a SAN in the certificate.)
[[ $(openssl x509 -in /conf/server.tls.crt -noout -text) == *DNS:* ]]

export SYNAPSE_TLS_CERT=/conf/server.tls.crt
export SYNAPSE_TLS_KEY=/conf/server.tls.key

# Add a directory for tests to add config overrides if they want
mkdir --parents /conf/homeserver.d
export _SYNAPSE_COMPLEMENT_EXTRA_CONFIG_DIR=/conf/homeserver.d

# ─── MAS setup ────────────────────────────────────────────────────────
if [[ "$SYNAPSE_COMPLEMENT_USE_MAS" == "true" ]]; then
  log "MAS integration enabled"

  # MAS requires postgres
  export START_POSTGRES=true
  export START_MAS=true

  # Generate secret for MAS <-> Synapse communication
  MAS_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

  # Hardcoded admin OAuth2 client credentials (test-only, ULID-compliant client_id)
  MAS_ADMIN_CLIENT_ID="01HGGCG3PCYWRNJSFQH1RQWQ4N"
  MAS_ADMIN_CLIENT_SECRET="complement-mas-shim-secret"

  # Start postgres temporarily to create the MAS database and run migrations
  log "Creating MAS database..."
  gosu postgres postgres -k /var/run/postgresql -D /var/lib/postgresql/data &
  PG_PID=$!
  # Wait for postgres to be ready
  for i in $(seq 1 30); do
    if gosu postgres pg_isready -q 2>/dev/null; then
      break
    fi
    sleep 0.5
  done
  gosu postgres psql -c "CREATE DATABASE mas" 2>/dev/null || true

  # Generate MAS config with proper keys using mas-cli
  log "Generating MAS config..."
  /mas/mas-cli config generate -o /mas/config.yaml

  # Patch the generated config with our settings using Python
  python3 <<PYEOF
import yaml

with open("/mas/config.yaml") as f:
    config = yaml.safe_load(f)

# HTTP settings
config["http"] = {
    "public_base": "http://localhost:8081/",
    "listeners": [{
        "name": "all",
        "resources": [
            {"name": "discovery"},
            {"name": "human"},
            {"name": "oauth"},
            {"name": "compat"},
            {"name": "graphql"},
            {"name": "assets"},
            {"name": "adminapi"},
        ],
        "binds": [{"address": "0.0.0.0:8081"}],
        "proxy_protocol": False,
    }],
}

# Database
config["database"] = {
    "uri": "postgresql://postgres:somesecret@localhost/mas",
}

# Matrix connection
config["matrix"] = {
    "kind": "synapse",
    "homeserver": "${SERVER_NAME}",
    "endpoint": "http://localhost:8008",
    "secret": "${MAS_SECRET}",
}

# Enable passwords
config["passwords"] = {
    "enabled": True,
    "minimum_complexity": 0,
}

# Enable password registration
config["account"] = {
    "password_registration_enabled": True,
}

# Add admin client
config["clients"] = config.get("clients", [])
config["clients"].append({
    "client_id": "${MAS_ADMIN_CLIENT_ID}",
    "client_secret": "${MAS_ADMIN_CLIENT_SECRET}",
    "client_auth_method": "client_secret_basic",
    "redirect_uris": [],
    "grant_types": ["client_credentials"],
    "response_types": [],
    "scope": "urn:mas:admin",
})

# Allow the admin client to use client_credentials with urn:mas:admin scope
config["policy"] = config.get("policy", {})
config["policy"]["data"] = {
    "admin_clients": ["${MAS_ADMIN_CLIENT_ID}"],
}

# Disable rate limiting for tests
config["rate_limiting"] = {
    "login": {
        "per_ip": {"burst": 1000, "per_second": 100.0},
        "per_account": {"burst": 1000, "per_second": 100.0},
    },
    "registration": {"burst": 1000, "per_second": 100.0},
}

with open("/mas/config.yaml", "w") as f:
    yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

print("MAS config patched successfully")
PYEOF

  # Run MAS database migrations (postgres is still running)
  log "Running MAS database migrations..."
  /mas/mas-cli database migrate --config /mas/config.yaml

  # Sync MAS config (registers clients)
  log "Syncing MAS config..."
  /mas/mas-cli config sync --config /mas/config.yaml

  # Stop postgres — supervisord will start it again properly
  kill $PG_PID 2>/dev/null
  wait $PG_PID 2>/dev/null || true

  # Write Synapse MAS integration config
  cat > /conf/homeserver.d/mas.yaml <<EOF
matrix_authentication_service:
  enabled: true
  endpoint: http://localhost:8081/
  secret: ${MAS_SECRET}
EOF

  log "MAS setup complete"
fi
# ─── End MAS setup ────────────────────────────────────────────────────

# Run the script that writes the necessary config files and starts supervisord, which in turn
# starts everything else
exec /configure_workers_and_start.py "$@"

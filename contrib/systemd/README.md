# Setup Synapse with Systemd
This is a setup for managing synapse with a user contributed systemd unit
file. It provides a `matrix-synapse` systemd unit file that should be tailored
to accommodate your installation in accordance with the installation
instructions provided in
[installation instructions](https://element-hq.github.io/synapse/latest/setup/installation.html).

## Setup
1. Under the service section, ensure the `User` variable matches which user
you installed synapse under and wish to run it as.
2. Under the service section, ensure the `WorkingDirectory` variable matches
where you have installed synapse.
3. Under the service section, ensure the `ExecStart` variable matches the
appropriate locations of your installation.
4. Copy the `matrix-synapse.service` to `/etc/systemd/system/`
5. Start Synapse: `sudo systemctl start matrix-synapse`
6. Verify Synapse is running: `sudo systemctl status matrix-synapse`
7. *optional* Enable Synapse to start at system boot: `sudo systemctl enable matrix-synapse`

## Systemd socket (optional)

To let systemd own the listening socket instead of Synapse binding it
directly, use `matrix-synapse.socket` alongside a `listeners` entry in
`homeserver.yaml` with `path: "systemd:matrix-synapse"` (the name must match
the socket unit's `FileDescriptorName=`, or its filename if unset). See the
[listener configuration
docs](https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#listeners)
for details.

## Logging

If you use `contrib/systemd/log_config.yaml`, install `systemd-python` in the
same Python environment as Synapse. The config uses
`systemd.journal.JournalHandler`, which requires that package.

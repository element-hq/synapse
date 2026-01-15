# How to monitor Synapse metrics using Prometheus

1.  Install Prometheus:

    Follow instructions at
    <http://prometheus.io/docs/introduction/install/>

1.  Enable Synapse metrics:

    In `homeserver.yaml`, make sure `enable_metrics` is
    set to `True`.

1.  Enable the `/_synapse/metrics` Synapse endpoint that Prometheus uses to
    collect data:

    There are two methods of enabling the metrics endpoint in Synapse.

    The first serves the metrics as a part of the usual web server and
    can be enabled by adding the `metrics` resource to the existing
    listener as such as in this example:

    ```yaml
    listeners:
      - port: 8008
        tls: false
        type: http
        x_forwarded: true
        bind_addresses: ['::1', '127.0.0.1']

        resources:
          # added "metrics" in this line
          - names: [client, federation, metrics]
            compress: false
    ```

    This provides a simple way of adding metrics to your Synapse
    installation, and serves under `/_synapse/metrics`. If you do not
    wish your metrics be publicly exposed, you will need to either
    filter it out at your load balancer, or use the second method.

    The second method runs the metrics server on a different port, in a
    different thread to Synapse. This can make it more resilient to
    heavy load meaning metrics cannot be retrieved, and can be exposed
    to just internal networks easier. The served metrics are available
    over HTTP only, and will be available at `/_synapse/metrics`.

    Add a new listener to homeserver.yaml as in this example:

    ```yaml
    listeners:
      - port: 8008
        tls: false
        type: http
        x_forwarded: true
        bind_addresses: ['::1', '127.0.0.1']

        resources:
          - names: [client, federation]
            compress: false

      # beginning of the new metrics listener
      - port: 9000
        type: metrics
        bind_addresses: ['::1', '127.0.0.1']
    ```

1.  Restart Synapse.

1.  Add a Prometheus target for Synapse.

    It needs to set the `metrics_path` to a non-default value (under
    `scrape_configs`):

    ```yaml
      - job_name: "synapse"
        scrape_interval: 15s
        metrics_path: "/_synapse/metrics"
        static_configs:
          - targets: ["my.server.here:port"]
    ```

    where `my.server.here` is the IP address of Synapse, and `port` is
    the listener port configured with the `metrics` resource.

    If your prometheus is older than 1.5.2, you will need to replace
    `static_configs` in the above with `target_groups`.

1.  Restart Prometheus.

1.  Consider using the [grafana dashboard](https://github.com/element-hq/synapse/tree/master/contrib/grafana/)
    and required [recording rules](https://github.com/element-hq/synapse/tree/master/contrib/prometheus/)

## Monitoring workers

To monitor a Synapse installation using [workers](workers.md),
every worker needs to be monitored independently, in addition to
the main homeserver process. This is because workers don't send
their metrics to the main homeserver process, but expose them
directly (if they are configured to do so).

To allow collecting metrics from a worker, you need to add a
`metrics` listener to its configuration, by adding the following
under `worker_listeners`:

```yaml
  - type: metrics
    bind_address: ''
    port: 9101
```

The `bind_address` and `port` parameters should be set so that
the resulting listener can be reached by prometheus, and they
don't clash with an existing worker.
With this example, the worker's metrics would then be available
on `http://127.0.0.1:9101`.

Example Prometheus target for Synapse with workers:

```yaml
  - job_name: "synapse"
    scrape_interval: 15s
    metrics_path: "/_synapse/metrics"
    static_configs:
      - targets: ["my.server.here:port"]
        labels:
          job: "master"
          index: 1
      - targets: ["my.workerserver.here:port"]
        labels:
          job: "generic_worker"
          index: 1
      - targets: ["my.workerserver.here:port"]
        labels:
          job: "generic_worker"
          index: 2
      - targets: ["my.workerserver.here:port"]
        labels:
          job: "media_repository"
          index: 1
```

Labels (`job`, `index`) can be defined as anything.
The labels are used to group graphs in grafana.

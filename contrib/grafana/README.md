# Using the Synapse Grafana dashboard

0. Set up Prometheus and Grafana. Out of scope for this readme. Useful documentation about using Grafana with Prometheus: http://docs.grafana.org/features/datasources/prometheus/
1. Have your Prometheus scrape your Synapse. https://element-hq.github.io/synapse/latest/metrics-howto.html
2. Import dashboard into Grafana. Download `synapse.json`. Import it to Grafana and select the correct Prometheus datasource. http://docs.grafana.org/reference/export_import/
3. Set up required recording rules. [contrib/prometheus](../prometheus)


## Sharing a JSON snapshot of a Grafana dashboard

To aid in debugging, you can share the dashboard with others by creating a snapshot of
the Grafana dashboard and exporting it as JSON. The snapshot will contain all of the
current values of the metrics visible on the dashboard.

**To capture the JSON snapshot:**

 1. Visit the Grafana dashboard in your browser
 1. Expand all of the sections on the dashboard and let all of the panels load in (the
    snapshot only captures what's loaded on your page)
 1. Use the Grafana UI to capture the snapshot: **Share** (drop down arrow) -> **Share
    Snapshot** -> **Publish Snapshot**
       - If you run into `413` (`Content Too Large`) errors, you're probably just running
         into the upload limit set on your reverse proxy (like nginx) in front of your
         Grafana instance. Just increase it and try again.
       - You may also run into `400` (`Bad Request`) which appear as `bad request data`
         in the Grafana UI if the snapshot is larger than 100 MB. Grafana introduced a
         [100 MB
         limit](https://github.com/grafana/grafana/blob/555d6dde60b0f49acd453c7293b1cd518fda3592/pkg/web/binding.go#L13-L14)
         as part of their [2026 June security
         releases](https://github.com/grafana/grafana/pull/125789). There is no
         workaround on this app limit, so you will have to reduce the time window,
         number of panels shown, etc.
 1. Grab the snapshot ID from the link generated in the last step or find it from the
    list of snapshots on https://localhost:3000/dashboard/snapshots
 1. To export the JSON, you have to use the [API for getting a
    snapshot](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/api-legacy/snapshot/#get-snapshot-by-key)
    (update the snapshot ID in the command below):
    ```shell
    curl --request GET \
      --header 'Content-Type: application/json' \
      --output ~/Downloads/2026-08-16-synapse-myhomeserver.com.json \
      http://admin:admin@localhost:3000/api/snapshots/nerimdSEDz530rM6CiwkEFi09A1841yF
    ```
 1. If you're trying to upload to GitHub, keep in mind that GitHub has a 25MB limit for
    attachments on issues. As an alternative, you could create a [GitHub
    Gist](https://gist.github.com/). If the snapshot is too big to upload via the GitHub
    UI, you can create a blank/empty gist and add it via git (gists are git repos).
 1. Once you have the JSON file, you can delete the snapshot from your Grafana instance
    to free up space (from the snaphots page,
    https://localhost:3000/dashboard/snapshots). The JSON file will still be valid and
    can be shared with others.

**To import the JSON snapshot into Grafana**, you have to use the [API for creating a snapshot](https://grafana.com/docs/grafana/latest/developer-resources/api-reference/http-api/snapshot/#create-new-snapshot) (passing in the whole JSON).

 1. Import example:
    ```shell
    cat ~/Downloads/2026-08-16-synapse-myhomeserver.com.json \
      | jq '. += {"name": "2026-08-16-synapse-myhomeserver.com"}' \
      | curl --request POST \
          --header 'Content-Type: application/json' \
          --data @- http://admin:admin@localhost:3000/api/snapshots
    ```
 1. Then you can find the snapshot on https://localhost:3000/dashboard/snapshots to view it.

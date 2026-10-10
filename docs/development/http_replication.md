# HTTP Replication

The term 'HTTP Replication' refers to HTTP requests made by Synapse workers and directed at other Synapse workers,
beginning with the path prefix `/_synapse/replication`.
Other systems would probably call these RPC (Remote Procedure Calls).

HTTP Replication is different (and complementary) to Redis Replication.
(HTTP Replication is for making a specific request at a specific worker, expecting a response,
whereas we use Redis Replication as a cross-worker publish/subscribe notification system,
with no concept of 'responses' and no direct targeting.)


## Compatibility during rolling upgrades of workers

In short: We don't guarantee or support replication compatibility during rolling upgrades.

(A 'rolling upgrade', sometimes called a 'rolling redeploy', is an upgrade where Synapse workers are gradually restarted,
such that during the upgrade process, some workers of the old Synapse version are running whilst other workers are running with the new version.
This is in contrast with a 'big bang' or 'stop the world' restart, where all Synapse workers on the old version are stopped
before any Synapse workers are started on the new version.)

When workers are running a mixture of versions, there can be disruption to availability (such as endpoints failing with
500 Internal Server Errors) and we accept that, so long as no lasting damage is done (such as database corruption)
and it self-heals after the upgrade completes.

In cases where it's easy to do so, we may, with best-effort only, support replication compatibility
between rolling upgrades of adjacent Synapse versions.
(This makes deployment to matrix.org easier, but it's not required.)

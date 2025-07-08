# Show scheduled tasks

This API returns information about scheduled tasks.

To use it, you will need to authenticate by providing an `access_token`
for a server admin: see [Admin API](../usage/administration/admin_api/).

The api is:
```
GET /_synapse/admin/v1/scheduled_tasks
```

It returns a JSON body like the following:

```json
{
    "scheduled_tasks": [
        {
            "id": "GSA124oegf1",
            "action": "shutdown_room",
            "status": "complete",
            "timestamp_ms": 23423523,
            "resource_id": "!roomid",
            "result": "some result",
            "error": null
        }
      ]
}
```

**Query parameters:**

* `action_name`: string - Is optional. Returns only the scheduled tasks with the given action name.
* `resource_id`: string - Is optional. Returns only the scheduled tasks with the given resource id.
* `status`: string - Is optional. Returns only the scheduled tasks matching the given status, one of
    - "scheduled" - Task is scheduled but not active
    - "active" - Task is active and probably running, and if not will be run on next scheduler loop run
    - "complete" - Task has completed successfully
    - "failed" - Task is over and either returned a failed status, or had an exception

* `max_timestamp`: int - Is optional. Returns only the scheduled tasks with a timestamp inferior to the specified one.

**Response**

The following fields are returned in the JSON response body along with a `200` HTTP status code:

* `id`: string - ID of scheduled task.
* `action`: string - The name of the scheduled task's action.
* `status`: string - The status of the scheduled task.
* `timestamp_ms`: integer - The timestamp (in milliseconds since the unix epoch) of the given task - If the status is "scheduled" then this represents when it should be launched.
  Otherwise it represents the last time this task got a change of state.
* `resource_id`: Optional string - The resource id of the scheduled task, if it possesses one
* `result`: Optional Json - Any result of the scheduled task, if given
* `error`: Optional string - If the task has the status "failed", the error associated with this failure

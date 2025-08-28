# Log Contexts

To help track the processing of individual requests, synapse uses a
`LoggingContext` to track which request it is handling at any given
moment. This is done via a `ContextVar` variable; a `logging.Filter` is
then used to fish the information back out of the `ContextVar` variable
and add it to each log record.

Log contexts are also used for CPU and database accounting, so that we
can track which requests were responsible for high CPU use or database
activity.

The `synapse.logging.context` module provides facilities for managing
the current log context (as well as providing the `LoggingContextFilter`
class).

In this document, "awaitable" refers to any object which can be `await`ed. In the
context of Synapse, that normally means either a coroutine or a Twisted
[`Deferred`](https://twistedmatrix.com/documents/current/api/twisted.internet.defer.Deferred.html).

## Basic usage

```python
from synapse.logging import context         # omitted from future snippets

def handle_request(request_id):
    request_context = context.LoggingContext()

    calling_context = context.set_current_context(request_context)
    try:
        request_context.request = request_id
        do_request_handling()
        logger.debug("finished")
    finally:
        context.set_current_context(calling_context)

def do_request_handling():
    logger.debug("phew")  # this will be logged against request_id
```

`LoggingContext` implements the context management methods, so the above
can be written much more succinctly as:

```python
def handle_request(request_id):
    with context.LoggingContext() as request_context:
        request_context.request = request_id
        do_request_handling()
        logger.debug("finished")

def do_request_handling():
    logger.debug("phew")
```

### The `sentinel` context

The default context is `context.SENTINEL_CONTEXT`, which is a sentinel value to
represent the root context. This is what is used when there is no other context set.

No CPU/database usage metrics are recorded against the `sentinel` context.

Ideally, nothing from the Synapse homeserver would be logged against the `sentinel`
context as we want to know where the logs came from. In practice, this is not always the
case yet especially outside of request handling.

Previously, the `sentinel` context played a bigger role when we had to carefully deal
with thread-local storage; as we had to make sure to not leak another context to another
task after we gave up control to the reactor so we set the 



### `PreserveLoggingContext`

In a similar vein of no longer as relevant, `PreserveLoggingContext` is another context
manager helper and a little bit of syntactic sugar to set the current log context
(without finishing it) and restore the previous context on exit.

```python
import logging
from synapse.logging.context import LoggingContext

logger = logging.getLogger(__name__)

def main() -> None:
    with context.LoggingContext("main"):
        task_context = context.LoggingContext("task")

        with task_context:
            logger.debug("foo")

        # Bad: will throw an error because `task_context` is already finished
        with task_context:
            logger.debug("bar")

        logger.debug("finished")
```

This can be fixed by using `PreserveLoggingContext`:

```python
import logging
from synapse.logging.context import LoggingContext

logger = logging.getLogger(__name__)

def main() -> None:
    with context.LoggingContext("main"):
        task_context = context.LoggingContext("task")

        with PreserveLoggingContext(task_context):
            logger.debug("foo")
        with PreserveLoggingContext(task_context):
            logger.debug("bar")

        logger.debug("finished") # this will be logged against main
```

Or you could equivalently just manage the log context manually via
`set_current_context`.


## Fire-and-forget

To drive an awaitable in the background, you can use `context.run_in_background`:

```python
async def do_request_handling():
    await foreground_operation()

    context.run_in_background(background_operation)

    # this will now be logged against the request context
    logger.debug("Request handling complete")
```

```python
async def do_request_handling():
    a1 = context.run_in_background(operation1)
    a2 = context.run_in_background(operation2)

    result = await defer.gatherResults([a1, a2])
```

`background_process_metrics.run_as_background_process` also exists if you want some
automatic tracing and metrics for the background task.

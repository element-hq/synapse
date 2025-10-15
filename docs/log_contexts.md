# Log Contexts

To help track the processing of individual requests, synapse uses a
'`log context`' to track which request it is handling at any given
moment. This is done via a thread-local variable; a `logging.Filter` is
then used to fish the information back out of the thread-local variable
and add it to each log record.

Logcontexts are also used for CPU and database accounting, so that we
can track which requests were responsible for high CPU use or database
activity.

The `synapse.logging.context` module provides facilities for managing
the current log context (as well as providing the `LoggingContextFilter`
class).

Asynchronous functions make the whole thing complicated, so this document describes
how it all works, and how to write code which follows the rules.

In this document, "awaitable" refers to any object which can be `await`ed. In the context of
Synapse, that normally means either a coroutine or a Twisted 
[`Deferred`](https://twistedmatrix.com/documents/current/api/twisted.internet.defer.Deferred.html).

## Logcontexts without asynchronous code

In the absence of any asynchronous voodoo, things are simple enough. As with
any code of this nature, the rule is that our function should leave
things as it found them:

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

LoggingContext implements the context management methods, so the above
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

The default logcontext is `synapse.logging.context.SENTINEL_CONTEXT`, which is an empty
sentinel value to represent the root logcontext. This is what is used when there is no
other logcontext set. The phrase "clear/reset the logcontext" means to set the current
logcontext to the `sentinel` logcontext.

No CPU/database usage metrics are recorded against the `sentinel` logcontext.

Ideally, nothing from the Synapse homeserver would be logged against the `sentinel`
logcontext as we want to know which server the logs came from. In practice, this is not
always the case yet especially outside of request handling.

Global things outside of Synapse (e.g. Twisted reactor code) should run in the
`sentinel` logcontext. It's only when it calls into application code that a logcontext
gets activated. This means the reactor should be started in the `sentinel` logcontext,
and any time an awaitable yields control back to the reactor, it should reset the
logcontext to be the `sentinel` logcontext. This is important to avoid leaking the
current logcontext to the reactor (which would then get picked up and associated with
the next thing the reactor does).


## Using logcontexts with awaitables

Awaitables break the linear flow of code so that there is no longer a single entry point
where we should set the logcontext and a single exit point where we should remove it.

Consider the example above, where `do_request_handling` needs to do some
blocking operation, and returns an awaitable:

```python
async def handle_request(request_id):
    with context.LoggingContext() as request_context:
        request_context.request = request_id
        await do_request_handling()
        logger.debug("finished")
```

In the above flow:

-   The logcontext is set
-   `do_request_handling` is called, and returns an awaitable
-   `handle_request` awaits the awaitable
-   Execution of `handle_request` is suspended

So we have stopped processing the request (and will probably go on to
start processing the next), without clearing the logcontext.

To circumvent this problem, synapse code assumes that, wherever you have
an awaitable, you will want to `await` it. To that end, wherever
functions return awaitables, we adopt the following conventions:

**Rules for functions returning awaitables:**

> -   If the awaitable is already complete, the function returns with the
>     same logcontext it started with.
> -   If the awaitable is incomplete, the function clears the logcontext
>     before returning; when the awaitable completes, it restores the
>     logcontext before running any callbacks.

That sounds complicated, but actually it means a lot of code (including
the example above) "just works". There are two cases:

-   If `do_request_handling` returns a completed awaitable, then the
    logcontext will still be in place. In this case, execution will
    continue immediately after the `await`; the "finished" line will
    be logged against the right context, and the `with` block restores
    the original context before we return to the caller.
-   If the returned awaitable is incomplete, `do_request_handling` clears
    the logcontext before returning. The logcontext is therefore clear
    when `handle_request` `await`s the awaitable.

    Once `do_request_handling`'s awaitable completes, it will reinstate
    the logcontext, before running the second half of `handle_request`,
    so again the "finished" line will be logged against the right context,
    and the `with` block restores the original context.

As an aside, it's worth noting that `handle_request` follows our rules
- though that only matters if the caller has its own logcontext which it
cares about.

The following sections describe pitfalls and helpful patterns when
implementing these rules.

## Always await your awaitables

Whenever you get an awaitable back from a function, you should `await` on
it as soon as possible. Do not pass go; do not do any logging; do not
call any other functions.

```python
async def fun():
    logger.debug("starting")
    await do_some_stuff()       # just like this

    coro = more_stuff()
    result = await coro         # also fine, of course

    return result
```

Provided this pattern is followed all the way back up to the callchain
to where the logcontext was set, this will make things work out ok:
provided `do_some_stuff` and `more_stuff` follow the rules above, then
so will `fun`.

It's all too easy to forget to `await`: for instance if we forgot that
`do_some_stuff` returned an awaitable, we might plough on regardless. This
leads to a mess; it will probably work itself out eventually, but not
before a load of stuff has been logged against the wrong context.
(Normally, other things will break, more obviously, if you forget to
`await`, so this tends not to be a major problem in practice.)

Of course sometimes you need to do something a bit fancier with your
awaitable - not all code follows the linear A-then-B-then-C pattern.
Notes on implementing more complex patterns are in later sections.

## Where you create a new awaitable, make it follow the rules

Most of the time, an awaitable comes from another synapse function.
Sometimes, though, we need to make up a new awaitable, or we get an awaitable
back from external code. We need to make it follow our rules.

The easy way to do it is by using `context.make_deferred_yieldable`. Suppose we want to implement
`sleep`, which returns a deferred which will run its callbacks after a
given number of seconds. That might look like:

```python
# not a logcontext-rules-compliant function
def get_sleep_deferred(seconds):
    d = defer.Deferred()
    reactor.callLater(seconds, d.callback, None)
    return d
```

That doesn't follow the rules, but we can fix it by calling it through
`context.make_deferred_yieldable`:

```python
async def sleep(seconds):
    return await context.make_deferred_yieldable(get_sleep_deferred(seconds))
```

## Deferred callbacks

When a deferred callback is called, it inherits the current logcontext. The deferred
callback chain can resume a coroutine, which if following our logcontext rules, will
restore its own logcontext, then run:

 - until it yields control back to the reactor, setting the sentinel logcontext
 - or until it finishes, restoring the logcontext it was started with (calling context)

This behavior creates two specific issues:

**Issue 1:** The first issue is that the callback may have reset the logcontext to the
sentinel before returning. This means our calling function will continue with the
sentinel logcontext instead of the logcontext it was started with (bad).

**Issue 2:** The second issue is that the current logcontext that called the deferred
callback could finish before the callback finishes (bad).

In the following example, the deferred callback is called with the "main" logcontext and
runs until we yield control back to the reactor in the `await` inside `clock.sleep(0)`.
Since `clock.sleep(0)` follows our logcontext rules, it sets the logcontext to the
sentinel before yielding control back to the reactor. Our `main` function continues with
the sentinel logcontext (first bad thing) instead of the "main" logcontext. Then the
`with LoggingContext("main")` block exits, finishing the "main" logcontext and yielding
control back to the reactor again. Finally, later on when `clock.sleep(0)` completes,
our `with LoggingContext("competing")` block exits, and restores the previous "main"
logcontext which has already finished, resulting in `WARNING: Re-starting finished log
context main` and leaking the `main` logcontext into the reactor which will then
erronously be associated with the next task the reactor picks up.

```python
async def competing_callback():
    # Since this is run with the "main" logcontext, when the "competing"
    # logcontext exits, it will restore the previous "main" logcontext which has
    # already finished and results in "WARNING: Re-starting finished log context main"
    # and leaking the `main` logcontext into the reactor.
    with LoggingContext("competing"):
        await clock.sleep(0)

def main():
    with LoggingContext("main"):
        d = defer.Deferred()
        d.addCallback(lambda _: defer.ensureDeferred(competing_callback()))
        # Call the callback within the "main" logcontext.
        d.callback(None)
        # Bad: This will be logged against sentinel logcontext
        logger.debug("ugh")

main()
```

**Solution 1:** We could of course fix this by following the general rule of "always
await your awaitables":

```python
async def main():
    with LoggingContext("main"):
        d = defer.Deferred()
        d.addCallback(lambda _: defer.ensureDeferred(competing_callback()))
        d.callback(None)
        # Wait for `d` to finish before continuing so the "main" logcontext is
        # still active. This works because `d` already follows our logcontext
        # rules. If not, we would also have to use `make_deferred_yieldable(d)`.
        await d
        # Good: This will be logged against the "main" logcontext
        logger.debug("phew")
``` 

**Solution 2:** We could also fix this by surrounding the call to `d.callback` with a
`PreserveLoggingContext`, which will reset the logcontext to the sentinel before calling
the callback, and restore the "main" logcontext afterwards before continuing the `main`
function. This solves the problem because when the "competing" logcontext exits, it will
restore the sentinel logcontext which is never finished by its nature, so there is no
warning and no leakage into the reactor.

```python
async def main():
    with LoggingContext("main"):
        d = defer.Deferred()
        d.addCallback(lambda _: defer.ensureDeferred(competing_callback()))
        d.callback(None)
        with PreserveLoggingContext():
            # Call the callback with the sentinel logcontext.
            d.callback(None)
        # Good: This will be logged against the "main" logcontext
        logger.debug("phew")
```

**Solution 3:** But let's say you *do* want to run (fire-and-forget) the deferred
callback in the current context without running into issues:

We can solve the first issue by using `run_in_background(...)` to run the callback in
the current logcontext and it handles the magic behind the scenes of a) restoring the
calling logcontext before returning to the caller and b) resetting the logcontext to the
sentinel after the deferred completes and we yield control back to the reactor to avoid
leaking the logcontext into the reactor.

To solve the second issue, we can extend the lifetime of the "main" logcontext by
avoiding the `LoggingContext`'s context manager lifetime methods
(`__enter__`/`__exit__`). We can still set "main" as the current logcontext by using
`PreserveLoggingContext` and passing in the "main" logcontext.


```python
async def main():
    main_context = LoggingContext("main")
    with PreserveLoggingContext(main_context):
        d = defer.Deferred()
        d.addCallback(lambda _: defer.ensureDeferred(competing_callback()))
        # The whole lambda will be run in the "main" logcontext. But we're using
        # a trick to return the deferred `d` itself so that `run_in_background`
        # will wait on that to complete and reset the logcontext to the sentinel
        # when it does to avoid leaking the "main" logcontext into the reactor.
        run_in_background(lambda: (d.callback(None), d)[1])
        # Good: This will be logged against the "main" logcontext
        logger.debug("phew")

...

# Wherever possible, it's best to finish the logcontext by calling `__exit__` at some
# point. This allows us to catch bugs if we later try to erroneously restart a finished
# logcontext.
#
# Since the "main" logcontext stores the `LoggingContext.previous_context` when it is
# created, we can wrap this call in `PreserveLoggingContext()` to restore the correct
# previous logcontext. Our goal is to have the calling context remain unchanged after
# finishing the "main" logcontext.
with PreserveLoggingContext():
    # Finish the "main" logcontext
    with main_context:
        # Empty block - We're just trying to call `__exit__` on the "main" context
        # manager to finish it. We can't call `__exit__` directly as the code expects us
        # to `__enter__` before calling `__exit__` to `start`/`stop` things
        # appropriately. And in any case, it's probably best not to call the internal
        # methods directly.
        pass
```

The same thing applies if you have some deferreds stored somewhere which you want to
callback in the current logcontext.


### Deferred errbacks and cancellations

The same care should be taken when calling errbacks on deferreds. An errback and
callback act the same in this regard (see section above).

```python
d = defer.Deferred()
d.addErrback(some_other_function)
d.errback(failure)
```

Additionally, cancellation is the same as directly calling the errback with a
`twisted.internet.defer.CancelledError`:

```python
d = defer.Deferred()
d.addErrback(some_other_function)
d.cancel()
```




## Fire-and-forget

Sometimes you want to fire off a chain of execution, but not wait for
its result. That might look a bit like this:

```python
async def do_request_handling():
    await foreground_operation()

    # *don't* do this
    background_operation()

    logger.debug("Request handling complete")

async def background_operation():
    await first_background_step()
    logger.debug("Completed first step")
    await second_background_step()
    logger.debug("Completed second step")
```

The above code does a couple of steps in the background after
`do_request_handling` has finished. The log lines are still logged
against the `request_context` logcontext, which may or may not be
desirable. There are two big problems with the above, however. The first
problem is that, if `background_operation` returns an incomplete
awaitable, it will expect its caller to `await` immediately, so will have
cleared the logcontext. In this example, that means that 'Request
handling complete' will be logged without any context.

The second problem, which is potentially even worse, is that when the
awaitable returned by `background_operation` completes, it will restore
the original logcontext. There is nothing waiting on that awaitable, so
the logcontext will leak into the reactor and possibly get attached to
some arbitrary future operation.

There are two potential solutions to this.

One option is to surround the call to `background_operation` with a
`PreserveLoggingContext` call. That will reset the logcontext before
starting `background_operation` (so the context restored when the
deferred completes will be the empty logcontext), and will restore the
current logcontext before continuing the foreground process:

```python
async def do_request_handling():
    await foreground_operation()

    # start background_operation off in the empty logcontext, to
    # avoid leaking the current context into the reactor.
    with PreserveLoggingContext():
        background_operation()

    # this will now be logged against the request context
    logger.debug("Request handling complete")
```

Obviously that option means that the operations done in
`background_operation` would be not be logged against a logcontext
(though that might be fixed by setting a different logcontext via a
`with LoggingContext(...)` in `background_operation`).

The second option is to use `context.run_in_background`, which wraps a
function so that it doesn't reset the logcontext even when it returns
an incomplete awaitable, and adds a callback to the returned awaitable to
reset the logcontext. In other words, it turns a function that follows
the Synapse rules about logcontexts and awaitables into one which behaves
more like an external function --- the opposite operation to that
described in the previous section. It can be used like this:

```python
async def do_request_handling():
    await foreground_operation()

    context.run_in_background(background_operation)

    # this will now be logged against the request context
    logger.debug("Request handling complete")
```

## Passing synapse deferreds into third-party functions

A typical example of this is where we want to collect together two or
more awaitables via `defer.gatherResults`:

```python
a1 = operation1()
a2 = operation2()
a3 = defer.gatherResults([a1, a2])
```

This is really a variation of the fire-and-forget problem above, in that
we are firing off `a1` and `a2` without awaiting on them. The difference
is that we now have third-party code attached to their callbacks. Anyway
either technique given in the [Fire-and-forget](#fire-and-forget)
section will work.

Of course, the new awaitable returned by `gather` needs to be
wrapped in order to make it follow the logcontext rules before we can
yield it, as described in [Where you create a new awaitable, make it
follow the
rules](#where-you-create-a-new-awaitable-make-it-follow-the-rules).

So, option one: reset the logcontext before starting the operations to
be gathered:

```python
async def do_request_handling():
    with PreserveLoggingContext():
        a1 = operation1()
        a2 = operation2()
        result = await defer.gatherResults([a1, a2])
```

In this case particularly, though, option two, of using
`context.run_in_background` almost certainly makes more sense, so that
`operation1` and `operation2` are both logged against the original
logcontext. This looks like:

```python
async def do_request_handling():
    a1 = context.run_in_background(operation1)
    a2 = context.run_in_background(operation2)

    result = await make_deferred_yieldable(defer.gatherResults([a1, a2]))
```

## A note on garbage-collection of awaitable chains

It turns out that our logcontext rules do not play nicely with awaitable
chains which get orphaned and garbage-collected.

Imagine we have some code that looks like this:

```python
listener_queue = []

def on_something_interesting():
    for d in listener_queue:
        d.callback("foo")

async def await_something_interesting():
    new_awaitable = defer.Deferred()
    listener_queue.append(new_awaitable)

    with PreserveLoggingContext():
        await new_awaitable
```

Obviously, the idea here is that we have a bunch of things which are
waiting for an event. (It's just an example of the problem here, but a
relatively common one.)

Now let's imagine two further things happen. First of all, whatever was
waiting for the interesting thing goes away. (Perhaps the request times
out, or something *even more* interesting happens.)

Secondly, let's suppose that we decide that the interesting thing is
never going to happen, and we reset the listener queue:

```python
def reset_listener_queue():
    listener_queue.clear()
```

So, both ends of the awaitable chain have now dropped their references,
and the awaitable chain is now orphaned, and will be garbage-collected at
some point. Note that `await_something_interesting` is a coroutine, 
which Python implements as a generator function.  When Python
garbage-collects generator functions, it gives them a chance to 
clean up by making the `await` (or `yield`) raise a `GeneratorExit`
exception. In our case, that means that the `__exit__` handler of
`PreserveLoggingContext` will carefully restore the request context, but
there is now nothing waiting for its return, so the request context is
never cleared.

To reiterate, this problem only arises when *both* ends of a awaitable
chain are dropped. Dropping the the reference to an awaitable you're
supposed to be awaiting is bad practice, so this doesn't
actually happen too much. Unfortunately, when it does happen, it will
lead to leaked logcontexts which are incredibly hard to track down.


## Debugging logcontext issues

Debugging logcontext issues can be tricky as leaking or losing a logcontext will surface
downstream and can point to an unrelated part of the codebase. It's best to enable debug
logging for `synapse.logging.context.debug` (needs to be explicitly configured) and go
backwards in the logs from the point where the issue is observed to find the root cause.

`log.config.yaml`
```yaml
loggers:
    # Unlike other loggers, this one needs to be explicitly configured to see debug logs.
    synapse.logging.context.debug:
        level: DEBUG
```

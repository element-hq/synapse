# OpenTracing

## Background

OpenTracing is a semi-standard being adopted by a number of distributed
tracing platforms. It is a common api for facilitating vendor-agnostic
tracing instrumentation. That is, we can use the OpenTracing api and
select one of a number of tracer implementations to do the heavy lifting
in the background. Our current selected implementation is Jaeger.

OpenTracing is a tool which gives an insight into the causal
relationship of work done in and between servers. The servers each track
events and report them to a centralised server - in Synapse's case:
Jaeger. The basic unit used to represent events is the span. The span
roughly represents a single piece of work that was done and the time at
which it occurred. A span can have child spans, meaning that the work of
the child had to be completed for the parent span to complete, or it can
have follow-on spans which represent work that is undertaken as a result
of the parent but is not depended on by the parent to in order to
finish.

Since this is undertaken in a distributed environment a request to
another server, such as an RPC or a simple GET, can be considered a span
(a unit or work) for the local server. This causal link is what
OpenTracing aims to capture and visualise. In order to do this metadata
about the local server's span, i.e the 'span context', needs to be
included with the request to the remote.

It is up to the remote server to decide what it does with the spans it
creates. This is called the sampling policy and it can be configured
through Jaeger's settings.

For OpenTracing concepts see
<https://opentracing.io/docs/overview/what-is-tracing/>.

For more information about Jaeger's implementation see
<https://www.jaegertracing.io/docs/>

## Setting up OpenTracing

To receive OpenTracing spans, start up a Jaeger server. This can be done
using docker like so:

```sh
docker run -d --name jaeger \
  -p 6831:6831/udp \
  -p 6832:6832/udp \
  -p 5778:5778 \
  -p 16686:16686 \
  -p 14268:14268 \
  jaegertracing/all-in-one:1
```

By default, Synapse will publish traces to Jaeger on localhost.
If Jaeger is hosted elsewhere, point Synapse to the correct host by setting
`opentracing.jaeger_config.local_agent.reporting_host` [in the Synapse configuration](usage/configuration/config_documentation.md#opentracing-1)
or by setting the `JAEGER_AGENT_HOST` environment variable to the desired address.

Latest documentation is probably at
https://www.jaegertracing.io/docs/latest/getting-started.

## Enable OpenTracing in Synapse

OpenTracing is not enabled by default. It must be enabled in the
homeserver config by adding the `opentracing` option to your config file. You can find 
documentation about how to do this in the [config manual under the header 'Opentracing'](usage/configuration/config_documentation.md#opentracing).
See below for an example Opentracing configuration: 

```yaml
opentracing:
  enabled: true
  homeserver_whitelist:
    - "mytrustedhomeserver.org"
    - "*.myotherhomeservers.com"
```

## Homeserver whitelisting

The homeserver whitelist is configured using regular expressions. A list
of regular expressions can be given and their union will be compared
when propagating any spans contexts to another homeserver.

Though it's mostly safe to send and receive span contexts to and from
untrusted users since span contexts are usually opaque ids it can lead
to two problems, namely:

-   If the span context is marked as sampled by the sending homeserver
    the receiver will sample it. Therefore two homeservers with wildly
    different sampling policies could incur higher sampling counts than
    intended.
-   Sending servers can attach arbitrary data to spans, known as
    'baggage'. For safety this has been disabled in Synapse but that
    doesn't prevent another server sending you baggage which will be
    logged to OpenTracing's logs.

## Configuring Jaeger

Sampling strategies can be set as in this document:
<https://www.jaegertracing.io/docs/latest/sampling/>.

## Spans from Rust code

Synapse reports spans for work done in its Rust extension through the same
Jaeger agent, under the same service name, as the Python spans. It uses the
agent address from `jaeger_config.local_agent` (or the `JAEGER_AGENT_HOST` and
`JAEGER_AGENT_PORT` environment variables), so there is nothing extra to
configure. A Rust span with a Python parent follows the parent's sampling
decision. A trace that starts in Rust is sampled according to the `const` or
`probabilistic` sampler. Under any other sampler, such traces are not sampled.

The same reporter also sends the Python spans, in place of jaeger-client's own
reporter and of the optional `rust_python_jaeger_reporter` package, which is no
longer used. The rest of `jaeger_config` (the sampler, remote or not, the
throttler, `logging`) applies as before. Its `reporter_queue_size`,
`reporter_batch_size` and `reporter_flush_interval` options and jaeger-client's
`jaeger:reporter_*` metrics no longer apply: the Rust reporter queues up to
1000 spans, sends a batch at 100 spans, at 65000 bytes or after a second, and
exports `synapse_rust_tracing_*` metrics instead. If tracing in Rust cannot
start, jaeger-client's reporter sends the Python spans.

The Rust reporter's counters are exported as Prometheus metrics named
`synapse_rust_tracing_*`. A growing `synapse_rust_tracing_spans_dropped_total`
means spans are finishing faster than they can be sent.

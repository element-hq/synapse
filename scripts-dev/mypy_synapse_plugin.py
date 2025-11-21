#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

"""This is a mypy plugin for Synpase to deal with some of the funky typing that
can crop up, e.g the cache descriptors.
"""

import enum
from typing import Callable, Mapping

import attr
import mypy.types
from mypy.erasetype import remove_instance_last_known_values
from mypy.errorcodes import ErrorCode
from mypy.nodes import ARG_NAMED_OPT, ListExpr, NameExpr, TempNode, TupleExpr, Var
from mypy.plugin import (
    ClassDefContext,
    Context,
    FunctionLike,
    FunctionSigContext,
    MethodSigContext,
    MypyFile,
    Plugin,
)
from mypy.typeops import bind_self
from mypy.types import (
    AnyType,
    CallableType,
    Instance,
    NoneType,
    Options,
    TupleType,
    TypeAliasType,
    TypeVarType,
    UninhabitedType,
    UnionType,
)
from mypy_zope import plugin as mypy_zope_plugin
from pydantic.mypy import plugin as mypy_pydantic_plugin

PROMETHEUS_METRIC_MISSING_SERVER_NAME_LABEL = ErrorCode(
    "missing-server-name-label",
    "`SERVER_NAME_LABEL` required in metric",
    category="per-homeserver-tenant-metrics",
)

PROMETHEUS_METRIC_MISSING_FROM_LIST_TO_CHECK = ErrorCode(
    "metric-type-missing-from-list",
    "Every Prometheus metric type must be included in the `prometheus_metric_fullname_to_label_arg_map`.",
    category="per-homeserver-tenant-metrics",
)

PREFER_SYNAPSE_CLOCK_CALL_LATER = ErrorCode(
    "call-later-not-tracked",
    "Prefer using `synapse.util.Clock.call_later` instead of `reactor.callLater`",
    category="synapse-reactor-clock",
)

PREFER_SYNAPSE_CLOCK_LOOPING_CALL = ErrorCode(
    "prefer-synapse-clock-looping-call",
    "Prefer using `synapse.util.Clock.looping_call` instead of `task.LoopingCall`",
    category="synapse-reactor-clock",
)

PREFER_SYNAPSE_CLOCK_CALL_WHEN_RUNNING = ErrorCode(
    "prefer-synapse-clock-call-when-running",
    "Prefer using `synapse.util.Clock.call_when_running` instead of `reactor.callWhenRunning`",
    category="synapse-reactor-clock",
)

PREFER_SYNAPSE_CLOCK_ADD_SYSTEM_EVENT_TRIGGER = ErrorCode(
    "prefer-synapse-clock-add-system-event-trigger",
    "Prefer using `synapse.util.Clock.add_system_event_trigger` instead of `reactor.addSystemEventTrigger`",
    category="synapse-reactor-clock",
)

MULTIPLE_INTERNAL_CLOCKS_CREATED = ErrorCode(
    "multiple-internal-clocks",
    "Only one instance of `clock.Clock` should be created",
    category="synapse-reactor-clock",
)

UNTRACKED_BACKGROUND_PROCESS = ErrorCode(
    "untracked-background-process",
    "Prefer using `HomeServer.run_as_background_process` method over the bare `run_as_background_process`",
    category="synapse-tracked-calls",
)


class Sentinel(enum.Enum):
    # defining a sentinel in this way allows mypy to correctly handle the
    # type of a dictionary lookup and subsequent type narrowing.
    UNSET_SENTINEL = object()


@attr.s(auto_attribs=True)
class ArgLocation:
    keyword_name: str
    """
    The keyword argument name for this argument
    """
    position: int
    """
    The 0-based positional index of this argument
    """


prometheus_metric_fullname_to_label_arg_map: Mapping[str, ArgLocation | None] = {
    # `Collector` subclasses:
    "prometheus_client.metrics.MetricWrapperBase": ArgLocation("labelnames", 2),
    "prometheus_client.metrics.Counter": ArgLocation("labelnames", 2),
    "prometheus_client.metrics.Histogram": ArgLocation("labelnames", 2),
    "prometheus_client.metrics.Gauge": ArgLocation("labelnames", 2),
    "prometheus_client.metrics.Summary": ArgLocation("labelnames", 2),
    "prometheus_client.metrics.Info": ArgLocation("labelnames", 2),
    "prometheus_client.metrics.Enum": ArgLocation("labelnames", 2),
    "synapse.metrics.LaterGauge": ArgLocation("labelnames", 2),
    "synapse.metrics.InFlightGauge": ArgLocation("labels", 2),
    "synapse.metrics.GaugeBucketCollector": ArgLocation("labelnames", 2),
    "prometheus_client.registry.Collector": None,
    "prometheus_client.registry._EmptyCollector": None,
    "prometheus_client.registry.CollectorRegistry": None,
    "prometheus_client.process_collector.ProcessCollector": None,
    "prometheus_client.platform_collector.PlatformCollector": None,
    "prometheus_client.gc_collector.GCCollector": None,
    "synapse.metrics._gc.GCCounts": None,
    "synapse.metrics._gc.PyPyGCStats": None,
    "synapse.metrics._reactor_metrics.ReactorLastSeenMetric": None,
    "synapse.metrics.CPUMetrics": None,
    "synapse.metrics.jemalloc.JemallocCollector": None,
    "synapse.util.metrics.DynamicCollectorRegistry": None,
    "synapse.metrics.background_process_metrics._Collector": None,
    #
    # `Metric` subclasses:
    "prometheus_client.metrics_core.Metric": None,
    "prometheus_client.metrics_core.UnknownMetricFamily": ArgLocation("labels", 3),
    "prometheus_client.metrics_core.CounterMetricFamily": ArgLocation("labels", 3),
    "prometheus_client.metrics_core.GaugeMetricFamily": ArgLocation("labels", 3),
    "prometheus_client.metrics_core.SummaryMetricFamily": ArgLocation("labels", 3),
    "prometheus_client.metrics_core.InfoMetricFamily": ArgLocation("labels", 3),
    "prometheus_client.metrics_core.HistogramMetricFamily": ArgLocation("labels", 3),
    "prometheus_client.metrics_core.GaugeHistogramMetricFamily": ArgLocation(
        "labels", 4
    ),
    "prometheus_client.metrics_core.StateSetMetricFamily": ArgLocation("labels", 3),
    "synapse.metrics.GaugeHistogramMetricFamilyWithLabels": ArgLocation(
        "labelnames", 4
    ),
}
"""
Map from the fullname of the Prometheus `Metric`/`Collector` classes to the keyword
argument name and positional index of the label names. This map is useful because
different metrics have different signatures for passing in label names and we just need
to know where to look.

This map should include any metrics that we collect with Prometheus. Which corresponds
to anything that inherits from `prometheus_client.registry.Collector`
(`synapse.metrics._types.Collector`) or `prometheus_client.metrics_core.Metric`. The
exhaustiveness of this list is enforced by `analyze_prometheus_metric_classes`.

The entries with `None` always fail the lint because they don't have a `labelnames`
argument (therefore, no `SERVER_NAME_LABEL`), but we include them here so that people
can notice and manually allow via a type ignore comment as the source of truth
should be in the source code.
"""

# Unbound at this point because we don't know the mypy version yet.
# This is set in the `plugin(...)` function below.
MypyPydanticPluginClass: type[Plugin]
MypyZopePluginClass: type[Plugin]


class SynapsePlugin(Plugin):
    def __init__(self, options: Options):
        super().__init__(options)
        self.mypy_pydantic_plugin = MypyPydanticPluginClass(options)
        self.mypy_zope_plugin = MypyZopePluginClass(options)

    def set_modules(self, modules: dict[str, MypyFile]) -> None:
        """
        This is called by mypy internals. We have to override this to ensure it's also
        called for any other plugins that we're manually handling.

        Here is how mypy describes it:

        > [`self._modules`] can't be set in `__init__` because it is executed too soon
        > in `build.py`. Therefore, `build.py` *must* set it later before graph processing
        > starts by calling `set_modules()`.
        """
        super().set_modules(modules)
        self.mypy_pydantic_plugin.set_modules(modules)
        self.mypy_zope_plugin.set_modules(modules)

    def get_base_class_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        def _get_base_class_hook(ctx: ClassDefContext) -> None:
            # Run any `get_base_class_hook` checks from other plugins first.
            #
            # Unfortunately, because mypy only chooses the first plugin that returns a
            # non-None value (known-limitation, c.f.
            # https://github.com/python/mypy/issues/19524), we workaround this by
            # putting our custom plugin first in the plugin order and then calling the
            # other plugin's hook manually followed by our own checks.
            if callback := self.mypy_pydantic_plugin.get_base_class_hook(fullname):
                callback(ctx)
            if callback := self.mypy_zope_plugin.get_base_class_hook(fullname):
                callback(ctx)

            # Now run our own checks
            analyze_prometheus_metric_classes(ctx)

        return _get_base_class_hook

    def get_function_signature_hook(
        self, fullname: str
    ) -> Callable[[FunctionSigContext], FunctionLike] | None:
        # Strip off the unique identifier for classes that are dynamically created inside
        # functions. ex. `synapse.metrics.jemalloc.JemallocCollector@185` (this is the line
        # number)
        if "@" in fullname:
            fullname = fullname.split("@", 1)[0]

        # Look for any Prometheus metrics to make sure they have the `SERVER_NAME_LABEL`
        # label.
        if fullname in prometheus_metric_fullname_to_label_arg_map.keys():
            # Because it's difficult to determine the `fullname` of the function in the
            # callback, let's just pass it in while we have it.
            return lambda ctx: check_prometheus_metric_instantiation(ctx, fullname)

        if fullname == "twisted.internet.task.LoopingCall":
            return check_looping_call

        if fullname == "synapse.util.clock.Clock":
            return check_clock_creation

        if (
            fullname
            == "synapse.metrics.background_process_metrics.run_as_background_process"
        ):
            return check_background_process

        return None

    def get_method_signature_hook(
        self, fullname: str
    ) -> Callable[[MethodSigContext], CallableType] | None:
        if fullname.startswith(
            (
                "synapse.util.caches.descriptors.CachedFunction.__call__",
                "synapse.util.caches.descriptors._LruCachedFunction.__call__",
            )
        ):
            return cached_function_method_signature

        if fullname in (
            "synapse.util.caches.descriptors._CachedFunctionDescriptor.__call__",
            "synapse.util.caches.descriptors._CachedListFunctionDescriptor.__call__",
        ):
            return check_is_cacheable_wrapper

        if fullname in (
            "twisted.internet.interfaces.IReactorTime.callLater",
            "synapse.types.ISynapseThreadlessReactor.callLater",
            "synapse.types.ISynapseReactor.callLater",
        ):
            return check_call_later

        if fullname in (
            "twisted.internet.interfaces.IReactorCore.callWhenRunning",
            "synapse.types.ISynapseThreadlessReactor.callWhenRunning",
            "synapse.types.ISynapseReactor.callWhenRunning",
        ):
            return check_call_when_running

        if fullname in (
            "twisted.internet.interfaces.IReactorCore.addSystemEventTrigger",
            "synapse.types.ISynapseThreadlessReactor.addSystemEventTrigger",
            "synapse.types.ISynapseReactor.addSystemEventTrigger",
        ):
            return check_add_system_event_trigger

        return None


def check_clock_creation(ctx: FunctionSigContext) -> CallableType:
    """
    Ensure that the only `clock.Clock` instance is the one used by the `HomeServer`.
    This is so that the `HomeServer` can cancel any tracked delayed or looping calls
    during server shutdown.

    Args:
        ctx: The `FunctionSigContext` from mypy.
    """
    signature: CallableType = ctx.default_signature
    ctx.api.fail(
        "Expected the only `clock.Clock` instance to be the one used by the `HomeServer`. "
        "This is so that the `HomeServer` can cancel any tracked delayed or looping calls "
        "during server shutdown",
        ctx.context,
        code=MULTIPLE_INTERNAL_CLOCKS_CREATED,
    )

    return signature


def check_call_later(ctx: MethodSigContext) -> CallableType:
    """
    Ensure that the `reactor.callLater` callsites aren't used.

    `synapse.util.Clock.call_later` should always be used instead of `reactor.callLater`.
    This is because the `synapse.util.Clock` tracks delayed calls in order to cancel any
    outstanding calls during server shutdown. Delayed calls which are either short lived
    (<~60s) or frequently called and can be tracked via other means could be candidates for
    using `synapse.util.Clock.call_later` with `call_later_cancel_on_shutdown` set to
    `False`. There shouldn't be a need to use `reactor.callLater` outside of tests or the
    `Clock` class itself. If a need arises, you can use a type ignore comment to disable the
    check, e.g. `# type: ignore[call-later-not-tracked]`.

    Args:
        ctx: The `FunctionSigContext` from mypy.
    """
    signature: CallableType = ctx.default_signature
    ctx.api.fail(
        "Expected all `reactor.callLater` calls to use `synapse.util.Clock.call_later` "
        "instead. This is so that long lived calls can be tracked for cancellation during "
        "server shutdown",
        ctx.context,
        code=PREFER_SYNAPSE_CLOCK_CALL_LATER,
    )

    return signature


def check_looping_call(ctx: FunctionSigContext) -> CallableType:
    """
    Ensure that the `task.LoopingCall` callsites aren't used.

    `synapse.util.Clock.looping_call` should always be used instead of `task.LoopingCall`.
    `synapse.util.Clock` tracks looping calls in order to cancel any outstanding calls
    during server shutdown.

    Args:
        ctx: The `FunctionSigContext` from mypy.
    """
    signature: CallableType = ctx.default_signature
    ctx.api.fail(
        "Expected all `task.LoopingCall` instances to use `synapse.util.Clock.looping_call` "
        "instead. This is so that long lived calls can be tracked for cancellation during "
        "server shutdown",
        ctx.context,
        code=PREFER_SYNAPSE_CLOCK_LOOPING_CALL,
    )

    return signature


def check_call_when_running(ctx: MethodSigContext) -> CallableType:
    """
    Ensure that the `reactor.callWhenRunning` callsites aren't used.

    `synapse.util.Clock.call_when_running` should always be used instead of
    `reactor.callWhenRunning`.

    Since `reactor.callWhenRunning` is a reactor callback, the callback will start out
    with the sentinel logcontext. `synapse.util.Clock` starts a default logcontext as we
    want to know which server the logs came from.

    Args:
        ctx: The `FunctionSigContext` from mypy.
    """
    signature: CallableType = ctx.default_signature
    ctx.api.fail(
        (
            "Expected all `reactor.callWhenRunning` calls to use `synapse.util.Clock.call_when_running` instead. "
            "This is so all Synapse code runs with a logcontext as we want to know which server the logs came from."
        ),
        ctx.context,
        code=PREFER_SYNAPSE_CLOCK_CALL_WHEN_RUNNING,
    )

    return signature


def check_add_system_event_trigger(ctx: MethodSigContext) -> CallableType:
    """
    Ensure that the `reactor.addSystemEventTrigger` callsites aren't used.

    `synapse.util.Clock.add_system_event_trigger` should always be used instead of
    `reactor.addSystemEventTrigger`.

    Since `reactor.addSystemEventTrigger` is a reactor callback, the callback will start out
    with the sentinel logcontext. `synapse.util.Clock` starts a default logcontext as we
    want to know which server the logs came from.

    Args:
        ctx: The `FunctionSigContext` from mypy.
    """
    signature: CallableType = ctx.default_signature
    ctx.api.fail(
        (
            "Expected all `reactor.addSystemEventTrigger` calls to use `synapse.util.Clock.add_system_event_trigger` instead. "
            "This is so all Synapse code runs with a logcontext as we want to know which server the logs came from."
        ),
        ctx.context,
        code=PREFER_SYNAPSE_CLOCK_ADD_SYSTEM_EVENT_TRIGGER,
    )

    return signature


def check_background_process(ctx: FunctionSigContext) -> CallableType:
    """
    Ensure that calls to `run_as_background_process` use the `HomeServer` method.
    This is so that the `HomeServer` can cancel any running background processes during
    server shutdown.

    Args:
        ctx: The `FunctionSigContext` from mypy.
    """
    signature: CallableType = ctx.default_signature
    ctx.api.fail(
        "Prefer using `HomeServer.run_as_background_process` method over the bare "
        "`run_as_background_process`. This is so that the `HomeServer` can cancel "
        "any background processes during server shutdown",
        ctx.context,
        code=UNTRACKED_BACKGROUND_PROCESS,
    )

    return signature


def analyze_prometheus_metric_classes(ctx: ClassDefContext) -> None:
    """
    Cross-check the list of Prometheus metric classes against the
    `prometheus_metric_fullname_to_label_arg_map` to ensure the list is exhaustive and
    up-to-date.
    """

    fullname = ctx.cls.fullname
    # Strip off the unique identifier for classes that are dynamically created inside
    # functions. ex. `synapse.metrics.jemalloc.JemallocCollector@185` (this is the line
    # number)
    if "@" in fullname:
        fullname = fullname.split("@", 1)[0]

    if any(
        ancestor_type.fullname
        in (
            # All of the Prometheus metric classes inherit from the `Collector`.
            "prometheus_client.registry.Collector",
            "synapse.metrics._types.Collector",
            # And custom metrics that inherit from `Metric`.
            "prometheus_client.metrics_core.Metric",
        )
        for ancestor_type in ctx.cls.info.mro
    ):
        if fullname not in prometheus_metric_fullname_to_label_arg_map:
            ctx.api.fail(
                f"Expected {fullname} to be in `prometheus_metric_fullname_to_label_arg_map`, "
                f"but it was not found. This is a problem with our custom mypy plugin. "
                f"Please add it to the map.",
                Context(),
                code=PROMETHEUS_METRIC_MISSING_FROM_LIST_TO_CHECK,
            )


def check_prometheus_metric_instantiation(
    ctx: FunctionSigContext, fullname: str
) -> CallableType:
    """
    Ensure that the `prometheus_client` metrics include the `SERVER_NAME_LABEL` label
    when instantiated.

    This is important because we support multiple Synapse instances running in the same
    process, where all metrics share a single global `REGISTRY`. The `server_name` label
    ensures metrics are correctly separated by homeserver.

    There are also some metrics that apply at the process level, such as CPU usage,
    Python garbage collection, and Twisted reactor tick time, which shouldn't have the
    `SERVER_NAME_LABEL`. In those cases, use a type ignore comment to disable the
    check, e.g. `# type: ignore[missing-server-name-label]`.

    Args:
        ctx: The `FunctionSigContext` from mypy.
        fullname: The fully qualified name of the function being called,
            e.g. `"prometheus_client.metrics.Counter"`
    """
    # The true signature, this isn't being modified so this is what will be returned.
    signature = ctx.default_signature

    # Find where the label names argument is in the function signature.
    arg_location = prometheus_metric_fullname_to_label_arg_map.get(
        fullname, Sentinel.UNSET_SENTINEL
    )
    assert arg_location is not Sentinel.UNSET_SENTINEL, (
        f"Expected to find {fullname} in `prometheus_metric_fullname_to_label_arg_map`, "
        f"but it was not found. This is a problem with our custom mypy plugin. "
        f"Please add it to the map. Context: {ctx.context}"
    )
    # People should be using `# type: ignore[missing-server-name-label]` for
    # process-level metrics that should not have the `SERVER_NAME_LABEL`.
    if arg_location is None:
        ctx.api.fail(
            f"{signature.name} does not have a `labelnames`/`labels` argument "
            "(if this is untrue, update `prometheus_metric_fullname_to_label_arg_map` "
            "in our custom mypy plugin) and should probably have a type ignore comment, "
            "e.g. `# type: ignore[missing-server-name-label]`. The reason we don't "
            "automatically ignore this is the source of truth should be in the source code.",
            ctx.context,
            code=PROMETHEUS_METRIC_MISSING_SERVER_NAME_LABEL,
        )
        return signature

    # Sanity check the arguments are still as expected in this version of
    # `prometheus_client`. ex. `Counter(name, documentation, labelnames, ...)`
    #
    # `signature.arg_names` should be: ["name", "documentation", "labelnames", ...]
    if (
        len(signature.arg_names) < (arg_location.position + 1)
        or signature.arg_names[arg_location.position] != arg_location.keyword_name
    ):
        ctx.api.fail(
            f"Expected argument number {arg_location.position + 1} of {signature.name} to be `labelnames`/`labels`, "
            f"but got {signature.arg_names[arg_location.position]}",
            ctx.context,
        )
        return signature

    # Ensure mypy is passing the correct number of arguments because we are doing some
    # dirty indexing into `ctx.args` later on.
    assert len(ctx.args) == len(signature.arg_names), (
        f"Expected the list of arguments in the {signature.name} signature ({len(signature.arg_names)})"
        f"to match the number of arguments from the function signature context ({len(ctx.args)})"
    )

    # Check if the `labelnames` argument includes `SERVER_NAME_LABEL`
    #
    # `ctx.args` should look like this:
    # ```
    # [
    #     [StrExpr("name")],
    #     [StrExpr("documentation")],
    #     [ListExpr([StrExpr("label1"), StrExpr("label2")])]
    #     ...
    # ]
    # ```
    labelnames_arg_expression = (
        ctx.args[arg_location.position][0]
        if len(ctx.args[arg_location.position]) > 0
        else None
    )
    if isinstance(labelnames_arg_expression, (ListExpr, TupleExpr)):
        # Check if the `labelnames` argument includes the `server_name` label (`SERVER_NAME_LABEL`).
        for labelname_expression in labelnames_arg_expression.items:
            if (
                isinstance(labelname_expression, NameExpr)
                and labelname_expression.fullname == "synapse.metrics.SERVER_NAME_LABEL"
            ):
                # Found the `SERVER_NAME_LABEL`, all good!
                break
        else:
            ctx.api.fail(
                f"Expected {signature.name} to include `SERVER_NAME_LABEL` in the list of labels. "
                "If this is a process-level metric (vs homeserver-level), use a type ignore comment "
                "to disable this check.",
                ctx.context,
                code=PROMETHEUS_METRIC_MISSING_SERVER_NAME_LABEL,
            )
    else:
        ctx.api.fail(
            f"Expected the `labelnames` argument of {signature.name} to be a list of label names "
            f"(including `SERVER_NAME_LABEL`), but got {labelnames_arg_expression}. "
            "If this is a process-level metric (vs homeserver-level), use a type ignore comment "
            "to disable this check.",
            ctx.context,
            code=PROMETHEUS_METRIC_MISSING_SERVER_NAME_LABEL,
        )
        return signature

    return signature


def _get_true_return_type(signature: CallableType) -> mypy.types.Type:
    """
    Get the "final" return type of a callable which might return an Awaitable/Deferred.
    """
    if isinstance(signature.ret_type, Instance):
        # If a coroutine, unwrap the coroutine's return type.
        if signature.ret_type.type.fullname == "typing.Coroutine":
            return signature.ret_type.args[2]

        # If an awaitable, unwrap the awaitable's final value.
        elif signature.ret_type.type.fullname == "typing.Awaitable":
            return signature.ret_type.args[0]

        # If a Deferred, unwrap the Deferred's final value.
        elif signature.ret_type.type.fullname == "twisted.internet.defer.Deferred":
            return signature.ret_type.args[0]

    # Otherwise, return the raw value of the function.
    return signature.ret_type


def cached_function_method_signature(ctx: MethodSigContext) -> CallableType:
    """Fixes the `CachedFunction.__call__` signature to be correct.

    It already has *almost* the correct signature, except:

        1. the `self` argument needs to be marked as "bound";
        2. any `cache_context` argument should be removed;
        3. an optional keyword argument `on_invalidated` should be added.
        4. Wrap the return type to always be a Deferred.
    """

    # 1. Mark this as a bound function signature.
    signature: CallableType = bind_self(ctx.default_signature)

    # 2. Remove any "cache_context" args.
    #
    # Note: We should be only doing this if `cache_context=True` is set, but if
    # it isn't then the code will raise an exception when its called anyway, so
    # it's not the end of the world.
    context_arg_index = None
    for idx, name in enumerate(signature.arg_names):
        if name == "cache_context":
            context_arg_index = idx
            break

    arg_types = list(signature.arg_types)
    arg_names = list(signature.arg_names)
    arg_kinds = list(signature.arg_kinds)

    if context_arg_index:
        arg_types.pop(context_arg_index)
        arg_names.pop(context_arg_index)
        arg_kinds.pop(context_arg_index)

    # 3. Add an optional "on_invalidate" argument.
    #
    # This is a either
    # - a callable which accepts no input and returns nothing, or
    # - None.
    calltyp = UnionType(
        [
            NoneType(),
            CallableType(
                arg_types=[],
                arg_kinds=[],
                arg_names=[],
                ret_type=NoneType(),
                fallback=ctx.api.named_generic_type("builtins.function", []),
            ),
        ]
    )

    arg_types.append(calltyp)
    arg_names.append("on_invalidate")
    arg_kinds.append(ARG_NAMED_OPT)  # Arg is an optional kwarg.

    # 4. Ensure the return type is a Deferred.
    ret_arg = _get_true_return_type(signature)

    # This should be able to use ctx.api.named_generic_type, but that doesn't seem
    # to find the correct symbol for anything more than 1 module deep.
    #
    # modules is not part of CheckerPluginInterface. The following is a combination
    # of TypeChecker.named_generic_type and TypeChecker.lookup_typeinfo.
    sym = ctx.api.modules["twisted.internet.defer"].names.get("Deferred")  # type: ignore[attr-defined]
    ret_type = Instance(sym.node, [remove_instance_last_known_values(ret_arg)])

    signature = signature.copy_modified(
        arg_types=arg_types,
        arg_names=arg_names,
        arg_kinds=arg_kinds,
        ret_type=ret_type,
    )

    return signature


def check_is_cacheable_wrapper(ctx: MethodSigContext) -> CallableType:
    """Asserts that the signature of a method returns a value which can be cached.

    Makes no changes to the provided method signature.
    """
    # The true signature, this isn't being modified so this is what will be returned.
    signature: CallableType = ctx.default_signature

    if not isinstance(ctx.args[0][0], TempNode):
        ctx.api.note("Cached function is not a TempNode?!", ctx.context)  # type: ignore[attr-defined]
        return signature

    orig_sig = ctx.args[0][0].type
    if not isinstance(orig_sig, CallableType):
        ctx.api.fail("Cached 'function' is not a callable", ctx.context)
        return signature

    check_is_cacheable(orig_sig, ctx)

    return signature


def check_is_cacheable(
    signature: CallableType,
    ctx: MethodSigContext | FunctionSigContext,
) -> None:
    """
    Check if a callable returns a type which can be cached.

    Args:
        signature: The callable to check.
        ctx: The signature context, used for error reporting.
    """
    # Unwrap the true return type from the cached function.
    return_type = _get_true_return_type(signature)

    verbose = ctx.api.options.verbosity >= 1
    # TODO Technically a cachedList only needs immutable values, but forcing them
    # to return Mapping instead of Dict is fine.
    ok, note = is_cacheable(return_type, signature, verbose)

    if ok:
        message = f"function {signature.name} is @cached, returning {return_type}"
    else:
        message = f"function {signature.name} is @cached, but has mutable return value {return_type}"

    if note:
        message += f" ({note})"
    message = message.replace("builtins.", "").replace("typing.", "")

    if ok and note:
        ctx.api.note(message, ctx.context)  # type: ignore[attr-defined]
    elif not ok:
        ctx.api.fail(message, ctx.context, code=AT_CACHED_MUTABLE_RETURN)


# Immutable simple values.
IMMUTABLE_VALUE_TYPES = {
    "builtins.bool",
    "builtins.int",
    "builtins.float",
    "builtins.str",
    "builtins.bytes",
}

# Types defined in Synapse which are known to be immutable.
IMMUTABLE_CUSTOM_TYPES = {
    "synapse.synapse_rust.acl.ServerAclEvaluator",
    "synapse.synapse_rust.push.FilteredPushRules",
    # This is technically not immutable, but close enough.
    "signedjson.types.VerifyKey",
    "synapse.types.StrCollection",
}

# Immutable containers only if the values are also immutable.
IMMUTABLE_CONTAINER_TYPES_REQUIRING_IMMUTABLE_ELEMENTS = {
    "builtins.frozenset",
    "builtins.tuple",
    "typing.AbstractSet",
    "typing.Sequence",
    "immutabledict.immutabledict",
}

MUTABLE_CONTAINER_TYPES = {
    "builtins.set",
    "builtins.list",
    "builtins.dict",
}

AT_CACHED_MUTABLE_RETURN = ErrorCode(
    "synapse-@cached-mutable",
    "@cached() should have an immutable return type",
    "General",
)


def is_cacheable(
    rt: mypy.types.Type, signature: CallableType, verbose: bool
) -> tuple[bool, str | None]:
    """
    Check if a particular type is cachable.

    A type is cachable if it is immutable; for complex types this recurses to
    check each type parameter.

    Returns: a 2-tuple (cacheable, message).
        - cachable: False means the type is definitely not cacheable;
            true means anything else.
        - Optional message.
    """

    # This should probably be done via a TypeVisitor. Apologies to the reader!
    if isinstance(rt, AnyType):
        return True, ("may be mutable" if verbose else None)

    elif isinstance(rt, Instance):
        if (
            rt.type.fullname in IMMUTABLE_VALUE_TYPES
            or rt.type.fullname in IMMUTABLE_CUSTOM_TYPES
        ):
            # "Simple" types are generally immutable.
            return True, None

        elif rt.type.fullname == "typing.Mapping":
            # Generally mapping keys are immutable, but they only *have* to be
            # hashable, which doesn't imply immutability. E.g. Mapping[K, V]
            # is cachable iff K and V are cachable.
            return is_cacheable(rt.args[0], signature, verbose) and is_cacheable(
                rt.args[1], signature, verbose
            )

        elif rt.type.fullname in IMMUTABLE_CONTAINER_TYPES_REQUIRING_IMMUTABLE_ELEMENTS:
            # E.g. Collection[T] is cachable iff T is cachable.
            return is_cacheable(rt.args[0], signature, verbose)

        elif rt.type.fullname in MUTABLE_CONTAINER_TYPES:
            # Mutable containers are mutable regardless of their underlying type.
            return False, f"container {rt.type.fullname} is mutable"

        elif "attrs" in rt.type.metadata:
            # attrs classes are only cachable iff it is frozen (immutable itself)
            # and all attributes are cachable.
            frozen = rt.type.metadata["attrs"]["frozen"]
            if frozen:
                for attribute in rt.type.metadata["attrs"]["attributes"]:
                    attribute_name = attribute["name"]
                    symbol_node = rt.type.names[attribute_name].node
                    assert isinstance(symbol_node, Var)
                    assert symbol_node.type is not None
                    ok, note = is_cacheable(symbol_node.type, signature, verbose)
                    if not ok:
                        return False, f"non-frozen attrs property: {attribute_name}"
                # All attributes were frozen.
                return True, None
            else:
                return False, "non-frozen attrs class"

        elif rt.type.is_enum:
            # We assume Enum values are immutable
            return True, None
        else:
            # Ensure we fail for unknown types, these generally means that the
            # above code is not complete.
            return (
                False,
                f"Don't know how to handle {rt.type.fullname} return type instance",
            )

    elif isinstance(rt, TypeVarType):
        # We consider TypeVars immutable if they are bound to a set of immutable
        # types.
        if rt.values:
            for value in rt.values:
                ok, note = is_cacheable(value, signature, verbose)
                if not ok:
                    return False, f"TypeVar bound not cacheable {value}"
            return True, None

        return False, "TypeVar is unbound"

    elif isinstance(rt, NoneType):
        # None is cachable.
        return True, None

    elif isinstance(rt, (TupleType, UnionType)):
        # Tuples and unions are cachable iff all their items are cachable.
        for item in rt.items:
            ok, note = is_cacheable(item, signature, verbose)
            if not ok:
                return False, note
        # This discards notes but that's probably fine
        return True, None

    elif isinstance(rt, TypeAliasType):
        # For a type alias, check if the underlying real type is cachable.
        return is_cacheable(mypy.types.get_proper_type(rt), signature, verbose)

    elif isinstance(rt, UninhabitedType):
        # There is no return value, just consider it cachable. This is only used
        # in tests.
        return True, None

    else:
        # Ensure we fail for unknown types, these generally means that the
        # above code is not complete.
        return False, f"Don't know how to handle {type(rt).__qualname__} return type"


def plugin(version: str) -> type[SynapsePlugin]:
    global MypyPydanticPluginClass, MypyZopePluginClass
    # This is the entry point of the plugin, and lets us deal with the fact
    # that the mypy plugin interface is *not* stable by looking at the version
    # string.
    #
    # However, since we pin the version of mypy Synapse uses in CI, we don't
    # really care.
    MypyPydanticPluginClass = mypy_pydantic_plugin(version)
    MypyZopePluginClass = mypy_zope_plugin(version)
    return SynapsePlugin

from __future__ import absolute_import, division, print_function

import weakref

import torch

import pyro
import pyro.poutine as poutine
from pyro.util import ignore_jit_warnings, optional


class CompiledFunction(object):
    """
    Output type of :func:`pyro.ops.jit.trace`.

    Wrapper around the output of :func:`torch.jit.trace`
    that handles parameter plumbing.

    The actual PyTorch compilation artifact is stored in :attr:`compiled`.
    Call diagnostic methods on this attribute.
    """
    def __init__(self, fn, ignore_warnings=False):
        self.fn = fn
        self.compiled = {}  # len(args) -> callable
        self.ignore_warnings = ignore_warnings
        self._param_names = None

    def __call__(self, *args, **kwargs):
        argc = len(args)

        # if first time
        if argc not in self.compiled:
            # param capture
            with poutine.block():
                with poutine.trace(param_only=True) as first_param_capture:
                    self.fn(*args, **kwargs)

            self._param_names = list(set(first_param_capture.trace.nodes.keys()))
            unconstrained_params = tuple(pyro.param(name).unconstrained()
                                         for name in self._param_names)
            params_and_args = unconstrained_params + args
            weakself = weakref.ref(self)

            def compiled(*params_and_args):
                self = weakself()
                unconstrained_params = params_and_args[:len(self._param_names)]
                args = params_and_args[len(self._param_names):]
                constrained_params = {}
                for name, unconstrained_param in zip(self._param_names, unconstrained_params):
                    constrained_param = pyro.param(name)  # assume param has been initialized
                    assert constrained_param.unconstrained() is unconstrained_param
                    constrained_params[name] = constrained_param
                return poutine.replay(self.fn, params=constrained_params)(*args, **kwargs)

            with pyro.validation_enabled(False), optional(ignore_jit_warnings(), self.ignore_warnings):
                self.compiled[argc] = torch.jit.trace(compiled, params_and_args, check_trace=False)
        else:
            unconstrained_params = [pyro.param(name).unconstrained()
                                    for name in self._param_names]
            params_and_args = unconstrained_params + list(args)

        with poutine.block(hide=self._param_names):
            with poutine.trace(param_only=True) as param_capture:
                ret = self.compiled[argc](*params_and_args)

        for name in param_capture.trace.nodes.keys():
            if name not in self._param_names:
                raise NotImplementedError('pyro.ops.jit.trace assumes all params are created on '
                                          'first invocation, but found new param: {}'.format(name))

        return ret


def trace(fn=None, ignore_warnings=False):
    """
    Lazy replacement for :func:`torch.jit.trace` that works with
    Pyro functions that call :func:`pyro.param`.

    The actual compilation artifact is stored in the ``compiled`` attribute of
    the output. Call diagnostic methods on this attribute.

    Example::

        def model(x):
            scale = pyro.param("scale", torch.tensor(0.5), constraint=constraints.positive)
            return pyro.sample("y", dist.Normal(x, scale))

        @pyro.ops.jit.trace
        def model_log_prob_fn(x, y):
            cond_model = pyro.condition(model, data={"y": y})
            tr = pyro.poutine.trace(cond_model).get_trace(x)
            return tr.log_prob_sum()
    """
    if fn is None:
        return lambda fn: trace(fn, ignore_warnings=ignore_warnings)
    return CompiledFunction(fn, ignore_warnings=ignore_warnings)

from __future__ import absolute_import, division, print_function

import warnings

from torch.distributions import kl_divergence

from pyro.distributions.util import is_identically_zero, scale_and_mask
from pyro.infer.trace_elbo import Trace_ELBO
from pyro.infer.util import is_validation_enabled, torch_item
from pyro.util import warn_if_nan


def _check_mean_field_requirement(model_trace, guide_trace):
    """
    Checks that the guide and model sample sites are ordered identically.
    This is sufficient but not necessary for correctness.
    """
    model_sites = [name for name, site in model_trace.nodes.items()
                   if site["type"] == "sample" and name in guide_trace.nodes]
    guide_sites = [name for name, site in guide_trace.nodes.items()
                   if site["type"] == "sample" and name in model_trace.nodes]
    assert set(model_sites) == set(guide_sites)
    if model_sites != guide_sites:
        warnings.warn("Failed to verify mean field restriction on the guide. "
                      "To eliminate this warning, ensure model and guide sites "
                      "occur in the same order.\n" +
                      "Model sites:\n  " + "\n  ".join(model_sites) +
                      "Guide sites:\n  " + "\n  ".join(guide_sites))


def _check_fully_reparametrized(guide_site):
    log_prob, score_function_term, entropy_term = guide_site["score_parts"]
    fully_rep = (guide_site["fn"].has_rsample and not is_identically_zero(entropy_term) and
                 is_identically_zero(score_function_term))
    if not fully_rep:
        raise NotImplementedError("All distributions in the guide must be fully reparameterized.")


class TraceMeanField_ELBO(Trace_ELBO):
    """
    A trace implementation of ELBO-based SVI. This is currently the only
    ELBO estimator in Pyro that uses analytic KL divergences when those
    are available.

    In contrast to, e.g.,
    :class:`~pyro.infer.tracegraph_elbo.TraceGraph_ELBO` and
    :class:`~pyro.infer.tracegraph_elbo.Trace_ELBO` this estimator places
    restrictions on the dependency structure of the model and guide.
    In particular it assumes that the guide has a mean-field structure,
    i.e. that it factorizes across the different latent variables present
    in the guide. It also assumes that all of the latent variables in the
    guide are reparameterized. This latter condition is satisfied for, e.g.,
    the Normal distribution but is not satisfied for, e.g., the Categorical
    distribution.

    .. warning:: This estimator may give incorrect results if the mean-field
      condition is not satisfied.

    Note for advanced users:

    The mean field condition is a sufficient but not necessary condition for
    this estimator to be correct. The precise condition is that for every
    latent variable `z` in the guide, its parents in the model must not include
    any latent variables that are descendants of `z` in the guide. Here
    'parents in the model' and 'descendants in the guide' is with respect
    to the corresponding (statistical) dependency structure. For example, this
    condition is always satisfied if the model and guide have identical
    dependency structures.
    """
    def _get_trace(self, model, guide, *args, **kwargs):
        model_trace, guide_trace = super(TraceMeanField_ELBO, self)._get_trace(
            model, guide, *args, **kwargs)
        if is_validation_enabled():
            _check_mean_field_requirement(model_trace, guide_trace)
        return model_trace, guide_trace

    def loss(self, model, guide, *args, **kwargs):
        """
        :returns: returns an estimate of the ELBO
        :rtype: float

        Evaluates the ELBO with an estimator that uses num_particles many samples/particles.
        """
        elbo = 0.0
        for model_trace, guide_trace in self._get_traces(model, guide, *args, **kwargs):
            elbo_particle, _ = self._differentiable_loss_particle(model_trace, guide_trace)
            elbo += elbo_particle / self.num_particles

        loss = -elbo
        warn_if_nan(loss, "loss")
        return loss

    def _differentiable_loss_particle(self, model_trace, guide_trace):
        elbo_particle = 0

        for name, model_site in model_trace.nodes.items():
            if model_site["type"] == "sample":
                if model_site["is_observed"]:
                    elbo_particle = elbo_particle + model_site["log_prob_sum"]
                else:
                    guide_site = guide_trace.nodes[name]
                    if is_validation_enabled():
                        _check_fully_reparametrized(guide_site)

                    # use kl divergence if available, else fall back on sampling
                    try:
                        kl_qp = kl_divergence(guide_site["fn"], model_site["fn"])
                        kl_qp = scale_and_mask(kl_qp, scale=guide_site["scale"], mask=guide_site["mask"])
                        assert kl_qp.shape == guide_site["fn"].batch_shape
                        elbo_particle = elbo_particle - kl_qp.sum()
                    except NotImplementedError:
                        entropy_term = guide_site["score_parts"].entropy_term
                        elbo_particle = elbo_particle + model_site["log_prob_sum"] - entropy_term.sum()

        # handle auxiliary sites in the guide
        for name, guide_site in guide_trace.nodes.items():
            if guide_site["type"] == "sample" and name not in model_trace.nodes():
                assert guide_site["infer"].get("is_auxiliary")
                if is_validation_enabled():
                    _check_fully_reparametrized(guide_site)
                entropy_term = guide_site["score_parts"].entropy_term
                elbo_particle = elbo_particle - entropy_term.sum()

        return -torch_item(elbo_particle), -elbo_particle

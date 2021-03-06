from __future__ import absolute_import, division, print_function

import argparse
import logging

import pandas as pd
import torch

import data
import pyro
import pyro.distributions as dist
import pyro.poutine as poutine
from pyro.infer.mcmc import MCMC, NUTS

logging.basicConfig(format='%(message)s', level=logging.INFO)
pyro.enable_validation(True)
pyro.set_rng_seed(0)


def model(sigma):
    eta = pyro.sample('eta', dist.Normal(torch.zeros(data.J), torch.ones(data.J)))
    mu = pyro.sample('mu', dist.Normal(torch.zeros(1), 10 * torch.ones(1)))
    tau = pyro.sample('tau', dist.HalfCauchy(scale=25 * torch.ones(1)))

    theta = mu + tau * eta

    return pyro.sample("obs", dist.Normal(theta, sigma))


def conditioned_model(model, sigma, y):
    return poutine.condition(model, data={"obs": y})(sigma)


def main(args):
    nuts_kernel = NUTS(conditioned_model, jit_compile=args.jit,)
    posterior = MCMC(nuts_kernel,
                     num_samples=args.num_samples,
                     warmup_steps=args.warmup_steps,
                     num_chains=args.num_chains).run(model, data.sigma, data.y)
    marginal = posterior.marginal(sites=["mu", "tau", "eta"]).empirical
    marginal = torch.cat([d.enumerate_support() for d in marginal.values()], dim=-1).cpu().numpy()
    params = ['mu', 'tau', 'eta[0]', 'eta[1]', 'eta[2]', 'eta[3]', 'eta[4]', 'eta[5]', 'eta[6]', 'eta[7]']
    df = pd.DataFrame(marginal, columns=params).transpose()
    df_summary = df.apply(pd.Series.describe, axis=1)[["mean", "std", "25%", "50%", "75%"]]
    logging.info(df_summary)


if __name__ == '__main__':
    assert pyro.__version__.startswith('0.3.0')
    parser = argparse.ArgumentParser(description='Eight Schools MCMC')
    parser.add_argument('--num-samples', type=int, default=1000,
                        help='number of MCMC samples (default: 1000)')
    parser.add_argument('--num-chains', type=int, default=1,
                        help='number of parallel MCMC chains (default: 1)')
    parser.add_argument('--warmup-steps', type=int, default=1000,
                        help='number of MCMC samples for warmup (default: 1000)')
    parser.add_argument('--jit', action='store_true', default=False)
    args = parser.parse_args()

    main(args)

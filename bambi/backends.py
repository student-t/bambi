from abc import ABCMeta, abstractmethod
from bambi.priors import Prior
from bambi.results import MCMCResults, PyMC3ADVIResults
from bambi.external.six import string_types
import numpy as np
import theano
import pymc3 as pm
from pymc3.model import TransformedRV
try:
    import pystan as ps
except:
    ps = None


class BackEnd(object):

    '''
    Base class for BackEnd hierarchy.
    '''
    __metaclass__ = ABCMeta

    @abstractmethod
    def build(self):
        pass

    @abstractmethod
    def run(self):
        pass


class PyMC3BackEnd(BackEnd):

    '''
    PyMC3 model-fitting back-end.
    '''

    # Available link functions
    links = {
        'identity': lambda x: x,
        'logit': theano.tensor.nnet.sigmoid,
        'inverse': theano.tensor.inv,
        'log': theano.tensor.log
    }

    def __init__(self):

        self.reset()

    def reset(self):
        '''
        Reset PyMC3 model and all tracked distributions and parameters.
        '''
        self.model = pm.Model()
        self.mu = None

    def _build_dist(self, label, dist, **kwargs):
        ''' Build and return a PyMC3 Distribution. '''
        if isinstance(dist, string_types):
            if not hasattr(pm, dist):
                raise ValueError("The Distribution class '%s' was not "
                                 "found in PyMC3." % dist)
            dist = getattr(pm, dist)
        # Inspect all args in case we have hyperparameters

        def _expand_args(k, v, label):
            if isinstance(v, Prior):
                label = '%s_%s' % (label, k)
                return self._build_dist(label, v.name, **v.args)
            return v

        kwargs = {k: _expand_args(k, v, label) for (k, v) in kwargs.items()}
        return dist(label, **kwargs)

    def build(self, spec, reset=True):
        '''
        Compile the PyMC3 model from an abstract model specification.
        Args:
            spec (Model): A bambi Model instance containing the abstract
                specification of the model to compile.
            reset (bool): if True (default), resets the PyMC3BackEnd instance
                before compiling.
        '''
        if reset:
            self.reset()

        with self.model:

            self.mu = 0.

            for t in spec.terms.values():

                data = t.data
                label = t.name
                dist_name = t.prior.name
                dist_args = t.prior.args

                # Effects w/ hyperparameters (i.e., random effects)
                if isinstance(data, dict):
                    for level, level_data in data.items():
                        n_cols = level_data.shape[1]
                        mu_label = 'u_%s_%s' % (label, level)
                        u = self._build_dist(mu_label, dist_name,
                                             shape=n_cols, **dist_args)
                        self.mu += pm.math.dot(level_data, u)[:, None]
                else:
                    prefix = 'u_' if t.random else 'b_'
                    n_cols = data.shape[1]
                    coef = self._build_dist(prefix + label, dist_name,
                                            shape=n_cols, **dist_args)
                    self.mu += pm.math.dot(data, coef)[:, None]

            y = spec.y.data
            y_prior = spec.family.prior
            link_f = spec.family.link
            if not callable(link_f):
                link_f = self.links[link_f]
            y_prior.args[spec.family.parent] = link_f(self.mu)
            y_prior.args['observed'] = y
            y_like = self._build_dist(
                spec.y.name, y_prior.name, **y_prior.args)

            self.spec = spec

    def _get_transformed_vars(self):
        # here we determine which variables have been internally transformed
        # (e.g., sd_log). transformed vars are actually 'untransformed' from
        # the PyMC3 model's perspective, and it's the backtransformed (e.g.,
        # sd) variable that is 'transformed'. So the logic of this is to find
        # and remove 'untransformed' variables that have a 'transformed'
        # counterpart, in that 'untransformed' varname is the 'transformed'
        # varname plus some suffix (such as '_log' or '_interval')
        rvs = self.model.unobserved_RVs
        trans = set(var.name for var in rvs if isinstance(var, pm.model.TransformedRV))
        untrans = set(var.name for var in rvs) - trans
        untrans = set(x for x in untrans if not any([t in x for t in trans]))
        return [x for x in self.trace.varnames if x not in (trans | untrans)]

    def run(self, start=None, method='mcmc', init=None, n_init=10000,
            find_map=False, **kwargs):
        '''
        Run the PyMC3 MCMC sampler.
        Args:
            start: Starting parameter values to pass to sampler; see
                pm.sample() documentation for details.
            method: The method to use for fitting the model. By default,
                'mcmc', in which case the PyMC3 sampler will be used.
                Alternatively, 'advi', in which case the model will be fitted
                using  automatic differentiation variational inference as
                implemented in PyMC3.
            init: Initialization method (see PyMC3 sampler documentation).
                In PyMC3, this defaults to 'advi', but we set it to None.
            n_init: Number of initialization iterations if init = 'advi' or
                'nuts'. Default is kind of in PyMC3 for the kinds of models
                we expect to see run with bambi, so we lower it considerably.
            find_map (bool): whether or not to use the maximum a posteriori
                estimate as a starting point; passed directly to PyMC3.
            kwargs (dict): Optional keyword arguments passed onto the sampler.
        Returns: A PyMC3ModelResults instance.
        '''

        if method == 'mcmc':
            samples = kwargs.pop('samples', 1000)
            with self.model:
                if start is None and find_map:
                    start = pm.find_MAP()
                self.trace = pm.sample(samples, start=start, init=init,
                                       n_init=n_init, **kwargs)
            return MCMCResults(self.spec, self.trace,
                               transformed_vars=self._get_transformed_vars())

        elif method == 'advi':
            with self.model:
                self.advi_params = pm.variational.advi(start, **kwargs)
            return PyMC3ADVIResults(self.spec, self.advi_params,
                                    transformed_vars=self._get_transformed_vars())


class StanBackEnd(BackEnd):
    '''
    Stan/PyStan model-fitting back-end.
    '''

    dists = {
        'Normal': {'name': 'normal', 'args': ['#mu', '#sd']},
        'Cauchy': {'name': 'cauchy', 'args': ['#alpha', '#beta']},
        'HalfNormal': {'name': 'normal', 'args': ['0', '#sd'],
                       'bounds':'<lower=0>'},
        'HalfCauchy': {'name': 'cauchy', 'args': ['0', '#beta'],
                       'bounds': '<lower=0>'}
    }

    def __init__(self):
        if ps is None:
            raise ImportError("Could not import PyStan; please make sure it's "
                              "installed.")
        self.reset()

    def reset(self):
        '''
        Reset Stan model and all tracked distributions and parameters.
        '''
        self.parameters = []
        self.transformed_parameters = []
        self.data = []
        self.transformed_data = []
        self.X = {}
        self.model = []
        self.mu = []
        self._suppress_vars = []  # variables to suppress in output

    def build(self, spec, reset=True):
        '''
        Compile the Stan model from an abstract model specification.
        Args:
            spec (Model): A bambi Model instance containing the abstract
                specification of the model to compile.
            reset (bool): if True (default), resets the StanBackEnd instance
                before compiling.
        '''
        if reset:
            self.reset()

        n_cases = len(spec.y.data)

        def _map_dist(dist, **kwargs):
            ''' Maps PyMC3 distribution names and attrs in the Prior object
            to the corresponding Stan names and argument order. '''
            if dist not in self.dists:
                raise ValueError("There is no distribution named '%s' "
                                 "in Stan." % dist)

            stan_dist = self.dists[dist]
            dist_name = stan_dist['name']
            dist_args = stan_dist['args']
            dist_bounds = stan_dist.get('bounds', '')

            lookup_args = [a[1:] for a in dist_args if a.startswith('#')]
            missing = set(lookup_args) - set(list(kwargs.keys()))
            if missing:
                raise ValueError("The following mandatory parameters of "
                                 "the %s distribution are missing: %s." 
                                 % (dist, missing))

            # Named arguments to take from the Prior object are denoted with
            # a '#'; otherwise we take the value in the self.dists dict as-is.
            dp = [kwargs[p[1:]] if p.startswith('#') else p for p in dist_args]

            # Sometimes we get numpy arrays at this stage, so convert to float
            dp = [float(p[0]) if isinstance(p, np.ndarray) else p for p in dp]

            dist_term = '%s(%s);' % (dist_name, ', '.join([str(p) for p in dp]))

            return dist_term, dist_bounds

        def _add_data(name, data):
            ''' Add all model components that directly touch or relate to data.
            '''
            if n_cols == 1:
                stan_data = 'vector[%d]' % (n_cases)
            else:
                stan_data = 'matrix[%d, %d]' % (n_cases, n_cols)
            self.data.append('%s %s_data;' % (stan_data, name))
            self.X[name + '_data'] = data.squeeze().astype(float)
            self.mu.append('%s_data * %s' % (name, name))

        def _add_parameters(name, dist_name, n_cols, **dist_args):
            ''' Add all model components related to latent parameters. We
            handle these separately from the data components, as the parameters
            can have nested specifications (in the case of random effects). '''

            def _expand_args(k, v, name):
                if isinstance(v, Prior):
                    name = '%s_%s' % (name, k)
                    return _add_parameters(name, v.name, 1, **v.args)
                return v

            kwargs = {k: _expand_args(k, v, name) for (k, v) in dist_args.items()}
            _dist, _bounds = _map_dist(dist_name, **kwargs)

            if n_cols == 1:
                stan_par = 'real'
            else:
                stan_par = 'vector[%d]' % n_cols

            self.parameters.append('%s%s %s;' % (stan_par, _bounds, name))
            self.model.append('%s ~ %s;' % (name, _dist))
            return name

        for t in spec.terms.values():

            data = t.data
            label = t.name
            dist_name = t.prior.name
            dist_args = t.prior.args

            # Effects with hyperpriors (i.e., random effects)
            if isinstance(data, dict):
                for level, level_data in data.items():
                    n_cols = level_data.shape[1]
                    name = 'u_%s_%s' % (label, level)
                    _add_data(name, level_data)
                    _add_parameters(name, dist_name, n_cols, **dist_args)

            else:
                prefix = 'u_' if t.random else 'b_'
                n_cols = data.shape[1]
                name = prefix + label

                # Add to Stan model
                _add_data(name, data)
                _add_parameters(name, dist_name, n_cols, **dist_args)

        # yhat
        yhat = 'yhat = ' + ' + '.join(self.mu) + ';'
        self.transformed_parameters.append('vector[%d] yhat;' % n_cases)
        self.transformed_parameters.append(yhat)
        self._suppress_vars.append('yhat')

        # y
        self.data.append('vector[%d] y;' % n_cases)
        self.parameters.append('real<lower=0> sigma;')
        self.model.append('y ~ normal(yhat, sigma);')
        self.X['y'] = spec.y.data.squeeze()

        # Construct the stan script
        def format_block(name):
            key = name.replace(' ', '_')
            els = ''.join(['\t%s\n' % e for e in getattr(self, key)])
            return '%s {\n%s}\n' % (name, els)

        blocks = ['data', 'transformed data', 'parameters',
                  'transformed parameters', 'model']
        self.model_code = ''.join([format_block(bl) for bl in blocks])
        self.spec = spec
        self.stan_model = ps.StanModel(model_code=self.model_code)

    def run(self, samples=1000, chains=1, **kwargs):
        '''
        Run the Stan sampler.
        Args:
            samples (int): Number of samples to obtain (in each chain).
            chains (int): Number of chains to use.
            kwargs (dict): Optional keyword arguments passed onto the PyStan
                StanModel.sampling() call.
        Returns: A PyMC3ModelResults instance.
        '''
        self.fit = self.stan_model.sampling(data=self.X, iter=samples,
                                            chains=chains, **kwargs)
        return self._convert_to_multitrace()

    def _convert_to_multitrace(self):
        ''' Convert a PyStan stanfit4model object to a PyMC3 MultiTrace. '''

        result = self.fit.extract(permuted=True, inc_warmup=True)
        varnames = list(result.keys())
        straces = []
        n_chains = self.fit.sim['chains']
        n_samples = self.fit.sim['iter'] - self.fit.sim['warmup']
        dummy = pm.Model()  # Need to fool the NDArray

        for i in range(n_chains):
            _strace = pm.backends.NDArray(model=dummy)
            _strace.setup(n_samples, i)
            _strace.draw_idx = _strace.draws
            _strace.varnames = varnames
            for k, par in result.items():
                start = i * n_samples
                _strace.samples[k] = par[start:(start+n_samples)]
            straces.append(_strace)

        self.trace = pm.backends.base.MultiTrace(straces)
        return MCMCResults(self.spec, self.trace, self._suppress_vars)

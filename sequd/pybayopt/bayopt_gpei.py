import os
import time
import signal
import shutil
import numpy as np
import pandas as pd
from collections import OrderedDict

from spearmint.ExperimentGrid import GridMap
from spearmint.chooser import GPEIOptChooser as module
from .bayopt_base import BayoptBase

grid_size = 20000


def set_timeout(func):
    def handle(signum, frame):
        raise RuntimeError

    def to_do(*args, **kwargs):
        try:
            signal.signal(signal.SIGALRM, handle)
            signal.alarm(args[0].time_out)
            r = func(*args, **kwargs)
            signal.alarm(0)
            return r
        except RuntimeError:
            raise Exception()
    return to_do


class GPEIOPT(BayoptBase):
    """
    Interface of Gaussian Process - Expected Improvement (Bayesian Optimization).

    Parameters
    ----------
    :type  para_space: dict or list of dictionaries
    :param para_space: It has three types:

        Continuous:
            Specify `Type` as `continuous`, and include the keys of `Range` (a list with lower-upper elements pair) and
            `Wrapper`, a callable function for wrapping the values.
        Integer:
            Specify `Type` as `integer`, and include the keys of `Mapping` (a list with all the sortted integer elements).
        Categorical:
            Specify `Type` as `categorical`, and include the keys of `Mapping` (a list with all the possible categories).

    :type max_runs: int, optional, default=100
    :param max_runs: The maximum number of trials to be evaluated. When this values is reached,
        then the algorithm will stop.

    :type time_out: float, optional, default=10
    :param time_out: The time out threshold (in seconds) for generating the next run.

    :type  estimator: estimator object
    :param estimator: This is assumed to implement the scikit-learn estimator interface.

    :type  cv: cross-validation method, an sklearn object.
    :param cv: e.g., `StratifiedKFold` and KFold` is used.

    :type scoring: string, callable, list/tuple, dict or None, optional, default = None
    :param scoring: A sklearn type scoring function.
        If None, the estimator's default scorer (if available) is used. See the package `sklearn` for details.

    :type refit: boolean, or string, optional, default=True
    :param refit: It controls whether to refit an estimator using the best found parameters on the whole dataset.

    :type random_state: int, optional, default=0
    :param random_state: The random seed for optimization.

    :type verbose: boolean, optional, default=False
    :param verbose: It controls whether the searching history will be printed.


    Examples
    ----------
    >>> import numpy as np
    >>> from sklearn import svm
    >>> from sklearn import datasets
    >>> from sequd import GPEIOPT
    >>> from sklearn.model_selection import KFold
    >>> iris = datasets.load_iris()
    >>> ParaSpace = {'C':{'Type': 'continuous', 'Range': [-6, 16], 'Wrapper': np.exp2},
               'gamma': {'Type': 'continuous', 'Range': [-16, 6], 'Wrapper': np.exp2}}
    >>> estimator = svm.SVC()
    >>> cv = KFold(n_splits=5, random_state=0, shuffle=True)
    >>> clf = GPEIOPT(ParaSpace, max_runs=100, time_out=10,
                estimator=estimator, cv=cv, scoring=None, refit=None, random_state=0, verbose=False)
    >>> clf.fit(iris.data, iris.target)

    Attributes
    ----------
    :vartype best_score\_: float
    :ivar best_score\_: The best average cv score among the evaluated trials.

    :vartype best_params\_: dict
    :ivar best_params\_: Parameters that reaches `best_score_`.

    :vartype best_estimator\_: sklearn estimator
    :ivar best_estimator\_: The estimator refitted based on the `best_params_`.
        Not available if estimator = None or `refit=False`.

    :vartype search_time_consumed\_: float
    :ivar search_time_consumed\_: Seconds used for whole searching procedure.

    :vartype refit_time\_: float
    :ivar refit_time\_: Seconds used for refitting the best model on the whole dataset.
        Not available if estimator=None or `refit=False`.
    """

    def __init__(self, para_space, max_runs=100, time_out=10, estimator=None, cv=None,
                 scoring=None, refit=True, random_state=0, verbose=False):

        super(GPEIOPT, self).__init__(para_space, max_runs, verbose)

        self.cv = cv
        self.refit = refit
        self.scoring = scoring
        self.estimator = estimator
        self.random_state = random_state
        self.time_out = time_out
        self.method = "GPEI"

        self.variables = {}
        for item, values in self.para_space.items():
            if (values['Type'] == "continuous"):
                self.variables[item] = OrderedDict({'name': item,
                                                    'type': 'float',
                                                    'min': values['Range'][0],
                                                    'max': values['Range'][1],
                                                    'size': 1})
            elif (values['Type'] == "integer"):
                self.variables[item] = OrderedDict({'name': item,
                                                    'type': 'int',
                                                    'min': min(values['Mapping']),
                                                    'max': max(values['Mapping']),
                                                    'size': 1})
            elif (values['Type'] == "categorical"):
                self.variables[item] = OrderedDict({'name': item,
                                                    'type': 'enum',
                                                    'options': values['Mapping'],
                                                    'size': 1})

    @set_timeout
    def _spmint_opt(self, chooser, grid, values, grid_status):

        # The status of jobs, 0 - candidate, 1 - pending, 2 - complete.
        # Here we only have two status: 0 or 2 available.
        job_id = chooser.next(grid, np.squeeze(values), [],
                              np.nonzero(grid_status == 0)[0],
                              np.nonzero(grid_status == 1)[0],
                              np.nonzero(grid_status == 2)[0])
        return job_id

    def obj_func(self, cfg):
        next_params = pd.DataFrame(np.array([cfg]), columns=self.para_names)
        parameters = {}
        for item, values in self.para_space.items():
            if (values['Type'] == "continuous"):
                parameters[item] = values['Wrapper'](float(next_params[item].iloc[0]))
            elif (values['Type'] == "integer"):
                parameters[item] = int(next_params[item].iloc[0])
            elif (values['Type'] == "categorical"):
                parameters[item] = next_params[item][0]

        score = self.wrapper_func(parameters)
        logs_aug = parameters
        logs_aug.update({"score": score})
        logs_aug = pd.DataFrame(logs_aug, index=[self.iteration])
        self.logs = pd.concat([self.logs, logs_aug]).reset_index(drop=True)

        if self.verbose:
            self.pbar.update(1)
            self.iteration += 1
            self.pbar.set_description("Iteration %d:" % self.iteration)
            self.pbar.set_postfix_str("Current Best Score = %.5f" % (self.logs.loc[:, "score"].max()))
        return -score

    def _run(self, wrapper_func):
        """
        Main loop for searching the best hyperparameters.

        """
        file_dir = "./temp/" + str(time.time()) + str(np.random.rand(1)[0]) + "/"
        os.makedirs(file_dir)

        self.wrapper_func = wrapper_func
        chooser = module.init(file_dir, "mcmc_iters=10")
        vkeys = [k for k in self.variables]
        gmap = GridMap([self.variables[k] for k in vkeys], grid_size)
        grid = np.asarray(gmap.hypercube_grid(grid_size, 1))
        values = np.zeros(grid_size) + np.nan
        grid_status = np.zeros(grid.shape[0])
        for i in range(np.int(self.max_runs)):
            try:
                job_id = self._spmint_opt(chooser, grid, values, grid_status)
            except Exception:
                print('Spearmint Early Stop!')
                break

            if isinstance(job_id, tuple):
                (job_id, candidate) = job_id
                grid = np.vstack((grid, candidate))
                grid_status = np.append(grid_status, 2)
                values = np.append(values, np.zeros(1) + np.nan)
                job_id = grid.shape[0] - 1
            else:
                candidate = grid[job_id, :]
                grid_status[job_id] = 2

            next_params = gmap.unit_to_list(np.clip(candidate, 0, 1))
            values[job_id] = self.obj_func(next_params)

        shutil.rmtree(file_dir)

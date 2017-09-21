"""pybnf.algorithms: contains the Algorithm class and subclasses as well as support classes and functions"""


from distributed import as_completed
from distributed import Client

from .pset import Model
from .pset import PSet
from .pset import Trajectory
import numpy as np

import logging


class Result(object):
    """
    Container for the results of a single evaluation in the fitting algorithm
    """

    def __init__(self, paramset, simdata):
        """
        Instantiates a Result

        :param paramset: The parameters corresponding to this evaluation
        :type paramset: PSet
        :param simdata: The simulation results corresponding to this evaluation
        :type simdata: list of Data instances
        """
        self.pset = paramset
        self.simdata = simdata


class Job:
    """
    Container for information necessary to perform a single evaluation in the fitting algorithm
    """

    def __init__(self, model, params):
        """
        Instantiates a Job

        :param model: The model to evaluate
        :type model: Model
        :param params: The parameter set with which to evaluate the model
        :type params: PSet
        """
        self.model = model
        self.params = params

    def run_simulation(self):
        """Runs the simulation and reads in the result"""
        
        pass


class Algorithm(object):
    def __init__(self, exp_data, objective, config):
        """
        Instantiates an Algorithm with a set of experimental data and an objective function.  Also
        initializes a Trajectory instance to track the fitting progress, and performs various additional
        configuration that is consistent for all algorithms

        :param exp_data: List of experimental Data objects to be fit
        :type exp_data: iterable
        :param objective: The objective function
        :type objective: ObjectiveFunction
        :param config: Configuration dictionary
        :type config: dict
        """
        self.exp_data = exp_data
        self.objective = objective
        self.config = config
        self.trajectory = Trajectory()

        # Store a list of all Model objects. Change this as needed for compatibility with other parts
        self.model = [Model(model_file) for model_file in config['model']]

        # Generate a list of variable names
        self.variable_list = []
        for key in config:
            if type(key) == tuple:
                self.variable_list.append(key[1])

    def start_run(self):
        """
        Called by the scheduler at the start of a fitting run.
        Must return a list of PSets that the scheduler should run.

        :return: list of PSets
        """
        logging.info("Initializing algorithm")
        raise NotImplementedError("Subclasses must implement start_run()")

    def got_result(self, res):
        """
        Called by the scheduler when a simulation is completed, with the pset that was run, and the resulting simulation
        data

        :param res: result from the completed simulation
        :type res: Result
        :return: List of PSet(s) to be run next.
        """
        logging.info("Retrieved result")
        raise NotImplementedError("Subclasses must implement got_result()")

    def add_to_trajectory(self, res):
        """Adds information from a Result to the Trajectory instance"""

        score = self.objective.evaluate(res.simdata, self.exp_data)
        self.trajectory.add(res.pset, score)

    def make_job(self, params):
        """
        Creates a new Job using the specified params, and additional specifications that are already saved in the
        Algorithm object

        :param params:
        :type params: PSet
        :return: Job
        """
        return Job(self.model, params)

    def run(self):
        """Main loop for executing the algorithm"""
        client = Client()
        psets = self.start_run()
        jobs = [self.make_job(p) for p in psets]
        futures = [client.submit(job.run_simulation) for job in jobs]
        pool = as_completed(futures, with_results=True)
        while True:
            f, res = next(pool)
            self.add_to_trajectory(res)
            response = self.got_result(res)
            if response == 'STOP':
                logging.info("Stop criterion satisfied")
                break
            else:
                new_jobs = [self.make_job(ps) for ps in response]
                pool.update([client.submit(j.run_simulation) for j in new_jobs])
        logging.info("Fitting complete")
        client.close()


class ParticleSwarm(Algorithm):
    """
    Implements particle swarm optimization.

    The implementation roughly follows Moraes et al 2015, although is reorganized to better suit PyBNF's format.
    Note the global convergence criterion discussed in that paper is not used (would require too long a
    computation), and instead uses ????

    """

    def __init__(self, expdata, objective, config):

        # Former params that are now part of the config
        #variable_list, num_particles, max_evals, cognitive=1.5, social=1.5, w0=1.,
        #wf=0.1, nmax=30, n_stop=np.inf, absolute_tol=0., relative_tol=0.)
        """
        Initial configuration of particle swarm optimizer

        :param expdata: Data object
        :param objective: ObjectiveFunction object
        :param config: Configuration dictionary
        :type config: dict

        The config should contain the following definitions:

        population_size - Number of particles in the swarm
        max_iterations - Maximum number of iterations. More precisely, the max number of simulations run is this times
        the population size.
        cognitive - Acceleration toward the particle's own best
        social - Acceleration toward the global best
        particle_weight - Inertia weight of the particle (default 1)

        The following config parameters relate to the complicated method presented is Moraes et al for adjusting the
        inertia weight as you go. These are optional, and this feature will be disabled (by setting
        particle_weight_final = particle_weight) if these are not included.
        It remains to be seen whether this method is at all useful for our applications.

        particle_weight_final -  Inertia weight at the end of the simulation
        adaptive_n_max - Controls how quickly we approach wf - After nmax "unproductive" iterations, we are halfway from
        w0 to wf
        adaptive_n_stop - nd the entire run if we have had this many "unproductive" iterations (should be more than
        adaptive_n_max)
        adaptive_abs_tol - Tolerance for determining if an iteration was "unproductive". A run is unproductive if the
        change in global_best is less than absolute_tol + relative_tol * global_best
        adaptive_rel_tol - Tolerance 2 for determining if an iteration was "unproductive" (see above)

        """

        super(ParticleSwarm, self).__init__(expdata, objective, config)

        # Set default values for non-essential parameters.
        defaults = {'particle_weight': 1.0, 'adaptive_n_max': 30, 'adaptive_n_stop': np.inf, 'adaptive_abs_tol': 0.0,
                    'adaptive_rel_tol': 0.0}
        for d in defaults:
            if d not in config:
                config[d] = defaults[d]

        # This default value gets special treatment because if missing, it should take the value of particle_weight,
        # disabling the adaptive weight change entirely.
        if 'particle_weight_final' not in config:
            config['particle_weight_final'] = config['particle_weight']

        # Save config parameters
        self.c1 = config['cognitive']
        self.c2 = config['social']
        self.max_evals = config['population_size'] * config['max_iterations']

        self.num_particles = config['population_size']
        # Todo: Nice error message if a required key is missing

        self.w0 = config['particle_weight']

        self.wf = config['particle_weight_final']
        self.nmax = config['adaptive_n_max']
        self.n_stop = config['adaptive_n_stop']
        self.absolute_tol = config['adaptive_abs_tol']
        self.relative_tol = config['adaptive_rel_tol']

        self.nv = 0  # Counter that controls the current weight. Counts number of "unproductive" iterations.
        self.num_evals = 0  # Counter for the total number of results received

        # Initialize storage for the swarm data
        self.swarm = []  # List of lists of the form [PSet, velocity]. Velocity is stored as a dict with the same keys
        # as PSet
        self.pset_map = dict()  # Maps each PSet to it s particle number, for easy lookup.
        self.bests = [[None, np.inf]] * self.num_particles  # The best result for each particle: list of the
        # form [PSet, objective]
        self.global_best = [None, np.inf]  # The best result for the whole swarm
        self.last_best = np.inf

    def start_run(self):
        """
        Start the run by initializing n particles at random positions and velocities
        :return:
        """

        for i in range(self.num_particles):
            new_params = PSet({xi: np.random.uniform(0, 4) for xi in self.variable_list})
            # Todo: Smart way to initialize velocity?
            new_velocity = {xi: np.random.uniform(-1, 1) for xi in self.variable_list}
            self.swarm.append([new_params, new_velocity])
            self.pset_map[new_params] = i

        return [particle[0] for particle in self.swarm]

    def got_result(self, res):
        """
        Updates particle velocity and position after a simulation completes.

        :param res: Result object containing the run PSet and the resulting Data.
        :return:
        """

        paramset = res.pset
        simdata = res.simdata

        self.num_evals += 1

        if self.num_evals % self.num_particles == 0:
            # End of one "pseudoflight", check if it was productive.
            if (self.last_best != np.inf and
                    np.abs(self.last_best - self.global_best[1]) <
                    self.absolute_tol + self.relative_tol * self.last_best):
                self.nv += 1
            self.last_best = self.global_best[1]

        score = self.objective.evaluate(simdata, self.exp_data)
        p = self.pset_map.pop(paramset)  # Particle number

        # Update best scores if needed.
        if score < self.bests[p][1]:
            self.bests[p] = [paramset, score]
            if score < self.global_best[1]:
                self.global_best = [paramset, score]

        # Update own position and velocity
        # The order matters - updating velocity first seems to make the best use of our current info.
        w = self.w0 + (self.wf - self.w0) * self.nv / (self.nv + self.nmax)
        self.swarm[p][1] = {v:
                                w * self.swarm[p][1][v] + self.c1 * np.random.random() * (
                                self.bests[p][0][v] - self.swarm[p][0][v]) +
                                self.c2 * np.random.random() * (self.global_best[0][v] - self.swarm[p][0][v])
                            for v in self.variable_list}
        new_pset = PSet({v: self.swarm[p][0][v] + self.swarm[p][1][v] for v in self.variable_list},
                             allow_negative=True)  # Todo: Smarter handling of negative values
        self.swarm[p][0] = new_pset
        self.pset_map[new_pset] = p

        # Check for stopping criteria
        if self.num_evals >= self.max_evals or self.nv >= self.n_stop:
            return 'STOP'

        return [new_pset]

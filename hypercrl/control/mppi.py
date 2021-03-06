import torch
import time
import logging
from torch.distributions.multivariate_normal import MultivariateNormal

logger = logging.getLogger(__name__)


def _ensure_non_zero(cost, beta, factor):
    return torch.exp(-factor * (cost - beta))


class MPPI():
    """
    Model Predictive Path Integral control
    This implementation batch samples the trajectories and so scales well with the number of samples K.
    Implemented according to algorithm 2 in Williams et al., 2017
    'Information Theoretic MPC for Model-Based Reinforcement Learning',
    based off of https://github.com/ferreirafabio/mppi_pendulum
    """

    def __init__(self, dynamics, running_cost, nx, noise_sigma, num_samples=100, horizon=15, device="cpu",
                 num_iter=1,
                 terminal_state_cost=None,
                 lambda_=1.,
                 noise_mu=None,
                 u_min=None,
                 u_max=None,
                 u_init=None,
                 U_init=None,
                 step_dependent_dynamics=False,
                 dynamics_variance=None,
                 running_cost_variance=None,
                 sample_null_action=False):
        """
        :param dynamics: function(state, action) -> next_state (K x nx) taking in batch state (K x nx) and action (K x nu)
        :param running_cost: function(state, action) -> cost (K x 1) taking in batch state and action (same as dynamics)
        :param nx: state dimension
        :param noise_sigma: (nu x nu) control noise covariance (assume v_t ~ N(u_t, noise_sigma))
        :param num_samples: K, number of trajectories to sample
        :param horizon: T, length of each trajectory
        :param device: pytorch device
        :param terminal_state_cost: function(state) -> cost (K x 1) taking in batch state
        :param lambda_: temperature, positive scalar where larger values will allow more exploration
        :param noise_mu: (nu) control noise mean (used to bias control samples); defaults to zero mean
        :param u_min: (nu) minimum values for each dimension of control to pass into dynamics
        :param u_max: (nu) maximum values for each dimension of control to pass into dynamics
        :param u_init: (nu) what to initialize new end of trajectory control to be; defeaults to zero
        :param U_init: (T x nu) initial control sequence; defaults to noise
        :param step_dependent_dynamics: whether the passed in dynamics needs horizon step passed in (as 3rd arg)
        :param dynamics_variance: function(state) -> variance (K x nx) give variance of the state calcualted from dynamics
        :param running_cost_variance: function(variance) -> cost (K x 1) cost function on the state variances
        :param sample_null_action: Whether to explicitly sample a null action (bad for starting in a local minima)
        """
        self.d = device
        self.dtype = noise_sigma.dtype
        self.K = num_samples  # N_SAMPLES
        self.T = horizon  # TIMESTEPS
        self.num_iter = num_iter # num iterations

        # dimensions of state and control
        self.nx = nx
        self.nu = 1 if len(noise_sigma.shape) is 0 else noise_sigma.shape[0]
        self.lambda_ = lambda_

        if noise_mu is None:
            noise_mu = torch.zeros(self.nu, dtype=self.dtype)

        if u_init is None:
            u_init = torch.zeros_like(noise_mu)

        # handle 1D edge case
        if self.nu is 1:
            noise_mu = noise_mu.view(-1)
            noise_sigma = noise_sigma.view(-1, 1)

        # bounds
        self.u_min = u_min
        self.u_max = u_max
        # make sure if any of them is specified, both are specified
        if self.u_max is not None and self.u_min is None:
            self.u_min = -self.u_max
        if self.u_min is not None and self.u_max is None:
            self.u_max = -self.u_min
        if self.u_min is not None:
            self.u_min = self.u_min.to(device=self.d)
            self.u_max = self.u_max.to(device=self.d)

        self.noise_mu = noise_mu.to(self.d)
        self.noise_sigma = noise_sigma.to(self.d)
        self.noise_sigma_inv = torch.inverse(self.noise_sigma)
        self.noise_dist = MultivariateNormal(self.noise_mu, covariance_matrix=self.noise_sigma)
        # T x nu control sequence
        self.U = U_init
        self.u_init = u_init.to(self.d)

        if self.U is None:
            self.U = self.noise_dist.sample((self.T,))

        self.step_dependency = step_dependent_dynamics
        self.F = dynamics
        self.dynamics_variance = dynamics_variance
        self.running_cost = running_cost
        self.running_cost_variance = running_cost_variance
        self.terminal_state_cost = terminal_state_cost
        self.sample_null_action = sample_null_action
        self.state = None

        # sampled results from last command
        self.cost_total = None
        self.cost_total_non_zero = None
        self.omega = None
        self.states = None
        self.actions = None
        if self.dynamics_variance is not None and self.running_cost_variance is None:
            raise RuntimeError("Need to give running cost for variance when giving the dynamics variance")

    def _dynamics(self, state, u, t, task_id):
        return self.F(state, u, t, task_id) if self.step_dependency else self.F(state, u, task_id)

    def command(self, state, task_id, first_action=True):
        """
        :param state: (nx) or (K x nx) current state, or samples of states (for propagating a distribution of states)
        :returns action: (nu) best action
        """
        # shift command 1 time step
        self.U = torch.roll(self.U, -1, dims=0)
        self.U[-1] = self.u_init

        if not torch.is_tensor(state):
            state = torch.tensor(state)
        self.state = state.to(dtype=self.dtype, device=self.d)

        for i in range(self.num_iter):
            cost_total = self._compute_total_cost_batch(task_id)

            beta = torch.min(cost_total)
            self.cost_total_non_zero = _ensure_non_zero(cost_total, beta, 1 / self.lambda_)

            eta = torch.sum(self.cost_total_non_zero)
            self.omega = (1. / eta) * self.cost_total_non_zero
            for t in range(self.T):
                self.U[t] += torch.sum(self.omega.view(-1, 1) * self.noise[:, t], dim=0)

        if first_action:
            action = self.U[0]
        else:
            action = self.U[:]
        return action

    def reset(self):
        """
        Clear controller state after finishing a trial
        """
        self.U = self.noise_dist.sample((self.T,))

    def _compute_total_cost_batch(self, task_id):
        # parallelize sampling across trajectories
        self.cost_total = torch.zeros(self.K, device=self.d, dtype=self.dtype)

        # allow propagation of a sample of states (ex. to carry a distribution), or to start with a single state
        if self.state.shape == (self.K, self.nx):
            state = self.state
        else:
            state = self.state.view(1, -1).repeat(self.K, 1)

        # resample noise each time we take an action
        self.noise = self.noise_dist.sample((self.K, self.T))
        # broadcast own control to noise over samples; now it's K x T x nu
        self.perturbed_action = self.U + self.noise
        if self.sample_null_action:
            self.perturbed_action[self.K - 1] = 0
        # naively bound control
        self.perturbed_action = self._bound_action(self.perturbed_action)
        # bounded noise after bounding (some got cut off, so we don't penalize that in action cost)
        self.noise = self.perturbed_action - self.U
        action_cost = self.lambda_ * self.noise @ self.noise_sigma_inv

        self.states = []
        self.actions = []
        for t in range(self.T):
            u = self.perturbed_action[:, t]
            state = self._dynamics(state, u, t, task_id)
            self.cost_total += self.running_cost(state, u, t, task_id)
            if self.dynamics_variance is not None:
                self.cost_total += self.running_cost_variance(self.dynamics_variance(state))

            # Save total states/actions
            self.states.append(state)
            self.actions.append(u)

        # Actions is N x T x nu
        # States is N x T x nx
        self.actions = torch.stack(self.actions, dim=1)
        self.states = torch.stack(self.states, dim=1)

        # action perturbation cost
        perturbation_cost = torch.sum(self.perturbed_action * action_cost, dim=(1, 2))
        if self.terminal_state_cost:
            self.cost_total += self.terminal_state_cost(self.states, self.actions)
        self.cost_total += perturbation_cost
        return self.cost_total

    def _bound_action(self, action):
        if self.u_max is not None:
            for t in range(self.T):
                u = action[:, self._slice_control(t)]
                cu = torch.max(torch.min(u, self.u_max), self.u_min)
                action[:, self._slice_control(t)] = cu
        return action

    def _slice_control(self, t):
        return slice(t * self.nu, (t + 1) * self.nu)

    def get_rollouts(self, state, task_id, num_rollouts=1):
        """
            :param state: either (nx) vector or (num_rollouts x nx) for sampled initial states
            :param num_rollouts: Number of rollouts with same action sequence - for generating samples with stochastic
                                 dynamics
            :returns states: num_rollouts x T x nx vector of trajectories
        """
        state = state.view(-1, self.nx)
        if state.size(0) == 1:
            state = state.repeat(num_rollouts, 1)

        T = self.U.shape[0]
        states = torch.zeros((num_rollouts, T + 1, self.nx), dtype=self.U.dtype, device=self.U.device)
        states[:, 0] = state
        for t in range(T):
            states[:, t + 1] = self._dynamics(states[:, t].view(num_rollouts, -1), self.U[t].view(num_rollouts, -1), t, task_id)
        return states[:, 1:]


def run_mppi(mppi, env, retrain_dynamics, retrain_after_iter=50, iter=1000, render=True):
    dataset = torch.zeros((retrain_after_iter, mppi.nx + mppi.nu), dtype=mppi.U.dtype, device=mppi.d)
    total_reward = 0
    for i in range(iter):
        state = env.state.copy()
        command_start = time.perf_counter()
        action = mppi.command(state)
        elapsed = time.perf_counter() - command_start
        s, r, _, _ = env.step(action.cpu().numpy())
        total_reward += r
        logger.debug("action taken: %.4f cost received: %.4f time taken: %.5fs", action, -r, elapsed)
        if render:
            env.render()

        di = i % retrain_after_iter
        if di == 0 and i > 0:
            retrain_dynamics(dataset)
            # don't have to clear dataset since it'll be overridden, but useful for debugging
            dataset.zero_()
        dataset[di, :mppi.nx] = torch.tensor(state, dtype=mppi.U.dtype)
        dataset[di, mppi.nx:] = action
    return total_reward, dataset

class PDDM():
    """
    Based on https://github.com/google-research/pddm
    Deep Dynamics Model for Learning Dexterous Manipulation
    """
    def __init__(self, dynamics, cost, nx, na, horizon, num_samples, beta, 
                kappa, mag_noise, gpuid):

        ###########
        ## params
        ###########
        self.horizon = horizon
        self.N = num_samples
        self._dynamics = dynamics
        self.cost_func = cost
        self.gpuid = gpuid

        #############
        ## init mppi vars
        #############
        self.x_dim = nx
        self.ac_dim = na
        self.mppi_kappa = kappa
        self.sigma = mag_noise * torch.ones(self.ac_dim).to(gpuid)
        self.beta = beta
        self.mppi_mean = torch.zeros(self.horizon, self.ac_dim, device=gpuid)  #start mean at 0

    ###################################################################
    ###################################################################
    #### update action mean using weighted average of the actions (by their resulting scores)
    ###################################################################
    ###################################################################

    def reset(self):
        self.mppi_mean = torch.zeros(self.horizon, self.ac_dim, device=self.gpuid)  #start mean at 0

    def mppi_update(self, costs, all_samples):

        #########################
        ## how each sim's score compares to the best score
        ##########################
        S = torch.exp(-self.mppi_kappa * (costs - torch.min(costs)))  # [N,]
        denom = torch.sum(S) + 1e-10

        ##########################
        ## weight all actions of the sequence by that sequence's resulting reward
        ##########################
        S_shaped = S.unsqueeze_(1).unsqueeze_(2)
        weighted_actions = (all_samples * S)  #[N x H x acDim]
        self.mppi_mean = torch.sum(weighted_actions, 0) / denom

        ##########################
        ## return 1st element of the mean, which corresps to curr timestep
        ##########################
        return self.mppi_mean

    def command(self, state, task_id, first_action=True):
        # Convert to torch
        if not torch.is_tensor(state):
            state = torch.tensor(state, device=self.gpuid, dtype=torch.float32)
        # set the shape properly
        if state.shape != (self.N, self.x_dim):
            state = state.view(1, -1).repeat(self.N, 1)

        # remove the 1st entry of mean (mean from past timestep, that was just executed)
        # and copy last entry (starting point, for the next timestep)
        past_action = self.mppi_mean[0].clone()
        self.mppi_mean[:-1] = self.mppi_mean[1:].clone()

        ##############################################
        ## sample candidate action sequences
        ## by creating smooth filtered trajecs (noised around a mean)
        ##############################################

        #np.random.seed()  # to get different action samples for each rollout

        eps = torch.randn((self.N, self.horizon, self.ac_dim), device=self.gpuid) * self.sigma

        # actions = mean + noise... then smooth the actions temporally
        all_samples = eps.clone()
        for i in range(self.horizon):
            if(i==0):
                all_samples[:, i, :] = self.beta*(self.mppi_mean[i, :] + eps[:, i, :]) + (1-self.beta)*past_action
            else:
                all_samples[:, i, :] = self.beta*(self.mppi_mean[i, :] + eps[:, i, :]) + (1-self.beta)*all_samples[:, i-1, :]

        #################################################
        ### Get result of executing those candidate action sequences
        #################################################
        self.states = []
        cost_total = torch.zeros(self.N, device=self.gpuid)
        for i in range(self.horizon):
            u = all_samples[:, i, :]
            state = self._dynamics(state, u, task_id)
            
            self.states.append(state)
            cost = self.cost_func(state, u, i, task_id)
            cost_total += cost

        # uses all paths to update action mean (for horizon steps)
        # Note: mppi_update needs rewards, so pass in -costs
        selected_action = self.mppi_update(cost_total, all_samples)
        
        if first_action:
            return selected_action[0]
        else:
            return selected_action
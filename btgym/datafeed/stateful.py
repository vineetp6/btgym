###############################################################################
#
# Copyright (C) 2017 Andrew Muzikin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################

import logging
#logging.basicConfig(format='%(name)s: %(message)s')

import datetime
import random
from numpy.random import beta as random_beta
import math
import os

import backtrader.feeds as btfeeds
import pandas as pd

from .simple import BTgymDataset


class BTgymSequentialTrial(BTgymDataset):
    """
    Sequential Data Trials iterator.
    Enables sliding or expanding time-window training and testing for the dataset of time-ordered records.

    Single Trial is defined by:

    - support train and test intervals::

        [train_start_time <-> train_end_time], [test_start_time <-> test_end_time],
        such as:
        train_start_time < train_end_time + 1 = test_start_time < test_end_time,
        where `1` stands for next closest time.

    - number of train episodes to draw from train support interval;

    - number of test episodes to draw from test support interval;

    Sliding time-window data iterating:

    If training is started from the beginningg of the dataset, `train_start_time` is set to that of first record,
    for example, for the start of the year::

        Trial train interval: 19 days, 23:59:00; test interval: 2 days, 23:59:00;
        Train episodes per trial: 1000; test episodes per trial: 10, test_period: 50, iterating from 0-th

    Then first trial intervals will be (note that omitted data periods like holidays are excluded)::

        Training interval: 2016-01-03 17:01:00 <--> 2016-01-31 17:14:00;
        Testing  interval: 2016-01-31 17:15:00 <--> 2016-02-03 17:14:00;

    Since `reset_data()` method call, every call to `BTgymSequentialTrial.sample()` method will return randomly drawn
    train episode from train interval, until reached `test_period` number of samples (here -50). Than iterator `pauses
    training` and each next call to `sample()` will return randomly drawn episode from test interval,
    until again max. number is reached (here - 10).
    Train-test loop is repeated until max. number of `Trial` train samples is reached ( here - 1000).

    Next call to `sample()` will result in following: next `Trial` will be formed such as::

        train_start_time_next_trial = `test_end_time_previous_trial + 1

    i.e. `Trial` will be shifted by the duration of test period,
    than first train episode of the new `Trial` will be sampled and returned.

    Repeats until entire dataset is exhausted.

    Note that while train periods are overlapping, test periods form a partition.

    Here, next trial will be::

        Training @: 2016-01-06 00:00:00 <--> 2016-02-03 00:10:00;
        Testing  @: 2016-02-03 00:12:00 <--> 2016-02-08 00:13:00

    Expanding time-window data iterating:

    Differs from above in a way that trial interval start position is fixed at the beginning of dataset. Thus,
    trial support interval is expanding to the right and every subsequent trial is `longer` than previous one
    by amount of test interval.

    Episodes sampling:

    Episodes sampling is performed in such a way that entire episode duration lies within `Trial` interval.

    Experimental:
    Train episode start position within interval is drawn from beta-distribution with default parameters b_alpha=1,
    b_beta=1, i.e. uniform one.

    Beta-distribution makes skewed sampling possible , e.g.
    to give recent episodes higher probability of being sampled, e.g.:  b_alpha=10, b_beta=0.8.

    It can be set to anneal to uniform one in specified number of train episodes. Annealing is done by exponentially
    decaying alpha and beta parameters to 1.

    Test episodes are always sampled uniformly.

    See description at `BTgymTrialRandomIterator()` for motivation.
    """
    trial_params = dict(
        # Trial-sampling params:
        train_range=dict(  # Trial time range in days, hours, minutes:
            days=7,
            hours=0,
        ),
        test_range=dict(  # Test time period in days, hours, minutes:
            days=7,
            hours=0,
        ),
        train_samples=0,
        test_samples=0,
        test_period=100,
        trial_start_00=True,
        expanding=False,
        b_alpha=1.0,
        b_beta=1.0,
        b_anneal_steps=-1
    )

    def __init__(self, **kwargs):
        """
        Args:
            kwargs:             BTgymDataset specific kwargs.
            train_range:        dict. containing `Trial` train interval in: `days`[, `hours`][, `minutes`];
            test_range:         dict. containing `Trial` test interval in: `days`[, `hours`][, `minutes`];
            train_samples:      number of episodes to draw from single `Trial train interval`;
            test_samples:       number of episodes to draw from `Trial test interval` every `test period`;
            test_period:        draw test episodes after every `test_period` train samples;
            expanding:          bool, if True - use expanding-type Trials, sliding otherwise; def=False;
            b_alpha:            sampling beta-distribution alpha param; def=1;
            b_beta:             sampling beta-distribution beta param; def=1;
            b_anneal_steps:     if set, anneals beta-distribution to uniform one in 'b_anneal_steps' number
                                of train samples, numbering continuously for all `Trials`; def=-1 (disabled);
            trial_start_00:     `Trial` start time will be set to that of first record of the day (usually 00:00);


        Note:
            - Total number of `Trials` (cardinality) is inferred upon args given and overall dataset size.
        """
        self.params.update(self.trial_params)
        super(BTgymSequentialTrial, self).__init__(**kwargs)

        # Timedeltas:
        self.train_range_delta = datetime.timedelta(**self.train_range)
        self.test_range_delta = datetime.timedelta(**self.test_range)

        self.train_range_row = 0
        self.test_range_row = 0
        self.train_mean_row = 0

        self.test_range_row = 0
        self.test_mean_row = 0

        self.global_step = 0
        self.total_steps = 0
        self.total_trials = 0
        self.trial_num = 0
        self.train_sample_num = 0
        self.test_sample_num = 0
        self.total_samples = 0

    @staticmethod
    def lin_decay(step, param_0, max_steps):
        """
        Linear decay from param_0 to 1 in `max_steps`.
        """
        if max_steps > 0:
            if step <= max_steps:
                return ((1 - param_0) / max_steps) * step + param_0

            else:
                return 1.0

        else:
            return param_0

    @staticmethod
    def exp_decay(step, param_0, max_steps, gamma=3.5):
        """
        For given step <= max_steps returns exp-decayed value in [param_0, 1]; returns 1 if step > max_steps;
        gamma - steepness control.
        """
        if max_steps > 0:
            if step <= max_steps:
                step = 2 - step / max_steps
                return math.exp(step ** gamma - 2 ** gamma) * (param_0 - 1) + 1

            else:
                return 1.0

        else:
            return param_0

    def sample(self, **kwargs):
        """
        Randomly samples from iterating sequence of `Trial` train/test distributions.

        Sampling loop::

            - until Trial_sequence is exhausted or .reset():
                - sample next Trial in Trial_sequence;
                    - until predefined number of episodes has been drawn:
                        - randomly draw single episode from current Trial TRAIN distribution;
                        - if reached test_period train episodes:
                            - until predefined number of episodes has been drawn:
                                - draw single episode from current Trial TEST distribution;

        Args:
            kwargs:     not used.

        Returns:
            `BTgymDataset` instance containing episode data [and metadata].
        """
        try:
            assert self.is_ready

        except AssertionError:
            return 'Data not ready. Call .reset() first.'

        episode, trial_num, type, sample_num = self._trial_sample_sequential()
        episode.metadata['type'] = type  # 0 - train, 1 - test
        episode.metadata['trial_num'] = trial_num
        episode.metadata['sample_num'] = sample_num
        self.log.debug('Seq_Data_Iterator: sample is ready with metadata: {}'.format(episode.metadata))
        return episode

    def reset(self, global_step=0, total_steps=None, skip_frame=10):
        """
        [Re]starts sampling iterator from specified position.

        Args:
            global_step:    position in [0, total_steps] interval to start sampling from.
            total_steps:    max gym environmnet steps allowed for full sweep over `Trials`.
            skip_frame:     BTGym specific, such as: `total_btgym_dataset_steps = total_steps * skip_frame`.
        """
        try:
            assert not self.data.empty

        except (AssertionError, AttributeError) as e:
            self.read_csv()

        # Total gym-environment steps and step training starts with:
        if total_steps is not None:
            self.total_steps = total_steps
            self.global_step = global_step
            assert self.global_step < self.total_steps, 'Outer space jumps not supported. Got: global_step={} of {}.'.\
                format(self.global_step, self.total_steps)

        else:
            self.global_step = 0
            self.total_steps = -1

        # Trial train support interval in number of records:
        self.train_range_row = int( self.train_range_delta.total_seconds() / (self.timeframe * 60))

        # Trial test support interval in number of records:
        self.test_range_row = int( self.test_range_delta.total_seconds() / (self.timeframe * 60))

        # Infer cardinality of distribution over Trials:

        self.total_trials = int(
            (self.data.shape[0] - self.train_range_row) / self.test_range_row
        )

        assert self.total_trials > 0, 'Trial`s cardinality below 1. Hint: check data parameters consistency.'

        # Infer number of train samples to draw from each Trial distribution:
        if self.total_steps > 0:
            self.train_samples = int(self.total_steps / (self.total_trials * self.episode_num_records / skip_frame))

        else:
            self.log.warning('`reset_data()` got total_steps=None -> train_samples={}, iterating from 0'.
                             format(self.train_samples))

        assert self.train_samples > 0, 'Number of train samples per trial below 1. Hint: check parameters consistency.'
        assert self.test_samples >= 0, 'Size of test samples batch below 0. Hint: check parameters consistency.'

        assert self.b_alpha > 0 and self.b_beta > 0, 'Expected positive B-distribution alpha, beta; got: {}'.\
            format([self.b_alpha, self.b_beta])

        # Current trial to start with:
        self.trial_num = int(self.total_trials * self.global_step / self.total_steps)

        # Number of train samples sampled so far (fror B-distr. annealing):
        self.total_samples = self.trial_num * self.train_samples

        #print('self.train_range_delta:', self.train_range_delta.total_seconds())
        #print('self.train_range_row:', self.train_range_row)
        #print('self.test_range_delta:', self.test_range_delta)

        self.train_sample_num = 0
        self.test_sample_num = 0

        # Mean of first train-Trial:
        self.train_mean_row = int(self.train_range_row / 2) + self.test_range_row * self.trial_num
        #print('self.train_mean_row:', self.train_mean_row)

        # If trial_start_00 option set, get index of first record of that day:
        if self.trial_start_00:
            train_first_row = self.train_mean_row - int(self.train_range_row / 2) + 1
            train_first_day = self.data[train_first_row:train_first_row + 1].index[0]
            self.train_mean_row = self.data.index.get_loc(train_first_day.date(), method='nearest') + \
                                  int(self.train_range_row / 2)
            self.log.warning('Trial train start time adjusted to <00:00>')

        # Mean of first test-Trial:
        self.test_mean_row = self.train_mean_row + int((self.train_range_row + self.test_range_row) / 2) + 1
        #print('self.test_mean_row:', self.test_mean_row)

        if self.expanding:
            start_time = self.data.index[0]
            start_row = 0
            t_type='EXPANDING'

        else:
            start_time = self.data.index[self.train_mean_row - int(self.train_range_row / 2)]
            start_row = self.train_mean_row - int(self.train_range_row / 2)
            t_type = 'SLIDING'

        self.log.warning(
            (
                '\nTrial type: {}; [initial] train interval: {}; test interval: {}.' +
                '\nCardinality: {}; iterating from: {}.' +
                '\nTrain episodes per trial: {}, sampling from beta-distribution[a:{}, b:{}] on train interval.'+
                '\nSampling {} test episodes after every {} train ones.'
            ).format(
                t_type,
                self.train_range_delta,
                self.test_range_delta,
                self.total_trials,
                self.trial_num,
                self.train_samples,
                self.b_alpha,
                self.b_beta,
                self.test_samples,
                self.test_period,

            )
        )
        if self.b_anneal_steps > 0:
            self.log.warning('\nAnnealing beta-distribution to uniform one in {} train samples.'.format(self.b_anneal_steps))
        self.log.warning(
            '\nTrial #{}:\nTraining @: {} <--> {};\nTesting  @: {} <--> {}'.
            format(
                self.trial_num,
                start_time,
                self.data.index[self.train_mean_row + int(self.train_range_row / 2)],
                self.data.index[self.test_mean_row - int(self.test_range_row / 2)],
                self.data.index[self.test_mean_row + int(self.test_range_row / 2)],
            )
        )
        self.log.debug(
            'Trial #{} rows: training @: {} <--> {}; testing @: {} <--> {}'.
            format(
                self.trial_num,
                start_row,
                self.train_mean_row + int(self.train_range_row / 2),
                self.test_mean_row - int(self.test_range_row / 2),
                self.test_mean_row + int(self.test_range_row / 2),
            )
        )
        self.is_ready = True

    def _trial_sample_sequential(self):

        # Is it time to run tests?
        if self.train_sample_num != 0 and self.train_sample_num % self.test_period == 0:
            # Until not done with testing:
            if self.test_sample_num < self.test_samples:
                self.test_sample_num += 1
                self.log.debug('Test sample #{}'.format(self.test_sample_num))
                # Uniformly sample tests:
                return self._sample_interval(
                    interval=[
                        self.test_mean_row - int(self.test_range_row / 2),
                        self.test_mean_row + int(self.test_range_row / 2)
                    ],
                    b_alpha=1,
                    b_beta=1
                ), self.trial_num, True, self.test_sample_num

            else:
                self.test_sample_num = 0

        # Have we done with training on current Trial?
        if self.train_sample_num >= self.train_samples:
            self.trial_num += 1
            self.train_sample_num = 0
            self.train_mean_row += self.test_range_row
            assert self.trial_num <= self.total_trials, 'Trial`s sequence exhausted.'  # Todo: self.ready = False

            # If trial_start_00 option set, get index of first record of that day:
            if self.trial_start_00:
                train_first_row = self.train_mean_row - int(self.train_range_row / 2) + 1
                train_first_day = self.data[train_first_row:train_first_row + 1].index[0]
                self.train_mean_row = self.data.index.get_loc(train_first_day.date(), method='nearest') + \
                                      int(self.train_range_row / 2)
                self.log.debug('Trial train start time adjusted to <00:00> :{}'.format(self.train_mean_row))
            self.test_mean_row = self.train_mean_row + int((self.train_range_row + self.test_range_row) / 2) + 1

            if self.expanding:
                start_time = self.data.index[0]

            else:
                start_time = self.data.index[self.train_mean_row - int(self.train_range_row / 2)]

            self.log.warning(
                'Trial #{}:\nTraining @: {} <--> {};\nTesting  @: {} <--> {}'.
                format(
                    self.trial_num,
                    start_time,
                    self.data.index[self.train_mean_row + int(self.train_range_row / 2)],
                    self.data.index[self.test_mean_row - int(self.test_range_row / 2)],
                    self.data.index[self.test_mean_row + int(self.test_range_row / 2)],
                )
            )

        self.train_sample_num += 1
        self.total_samples += 1
        self.log.debug('Train sample #{}'.format(self.train_sample_num))

        if self.expanding:
            interval = [0, self.train_mean_row + int(self.train_range_row / 2)]

        else:
            interval = [
                self.train_mean_row - int(self.train_range_row / 2),
                self.train_mean_row + int(self.train_range_row / 2)
            ]
        return self._sample_interval(
            interval=interval,
            b_alpha=self.exp_decay(self.total_samples, self.b_alpha, self.b_anneal_steps),
            b_beta=self.exp_decay(self.total_samples, self.b_beta, self.b_anneal_steps),
        ), self.trial_num, False, self.train_sample_num


class BTgymRandomTrial(BTgymSequentialTrial):
    """
    Random Data Trials iterator.

    Note:
        While these iterators can simply be seen as sliding/random sampling time-windows, the realisation is inspired by
        `FAST REINFORCEMENT LEARNING VIA SLOW REINFORCEMENT LEARNING` paper by Duan et al.,
        https://arxiv.org/pdf/1611.02779.pdf

        Problem: Real-world BTGym POMDP violates condition of having stationary transitional distribution.

        Want: re-present BTGym on-line task as set [actually a well-ordered] of discrete-time finite-horizon discounted
        partially-observed Markov decision processes (POMDP's) to define optimization objective of learning
        RL algorithm itself, which can [hopefully] be one approach to learning in changing environment.

        Note that BTgym dataset is set of date_time ordered, mainly continuous records.
        Let each `Trial` be discrete uniform distribution among all episodes, such as for each episode:
        a) start time lies within particular `Trail support time interval` of `trial_range` time length and
        b) episode duration is less or equal to `max_episode_duration` constant;
        let `Trail mean` be particular row or date_time position within dataset timeline.

        Under original BTGym conditions, for each `Trial` there exists `single POMDP`.

        Let `Trial_sequence` be a set of `Trials` resulted by incrementing `trial_mean` parameter
        with `trial_stride` from end to end of dataset timeline.

        Such `Trial_sequence` casts a `set of POMDP's`, every element of wich can be considered well-defined in terms
        of own transition distribution; now it's possible to design optimization objective `...to maximize the expected
        total discounted reward accumulated during a single trial rather than a single episode.` [above paper, 2.2]

        This particular iterator casts unordered `set of Trials`, while `BTgymSequentialTrial()` class sweeps through
        later in time-ordered fashion.
    """

    def __init__(self, **kwargs):
        """
        Args:
            **kwargs:           BTgymDataset() specific kwargs.
            trial_range:        dict. containing `Trial support interval` (time range) as: `days`[, `hours`][, `minutes`].
            trial_stride:       dict. containing `stride interval` between `Trials` as: `days`[, `hours`][, `minutes`].
            samples_per_trial:  self-explaining; unlike sequential case, has to be set explicitly.
        """
        super(BTgymRandomTrial, self).__init__(**kwargs)
        self.trial_num = 0

    def reset(self, **kwargs):
        """
        [Re]starts sampling iterator.

        Args:
            kwargs:     not used.
        """
        try:
            assert not self.data.empty

        except (AssertionError, AttributeError) as e:
            self.read_csv()

        # Infer cardinality of distribution over Trials:
        self.total_trials = int((self.data_range_delta - self.train_range_delta) / self.test_range_delta)

        assert self.total_trials > 0, 'Trial`s cardinality below 1. Hint: check data parameters consistency.'

        # Current trial to start with:
        self.trial_num = 0

        # Trial support interval in number of records:
        self.trial_range_row = int(self.data.shape[0] * (self.train_range_delta / self.data_range_delta))

        # Sequential step size:
        self.trial_stride_row = int(self.data.shape[0] * (self.test_range_delta / self.data_range_delta))

        self.sample_num = 0

        # Mean of first Trial:
        self.trial_mean_row = int(self.trial_range_row / 2) + \
                              self.trial_stride_row * int(self.total_trials * random.random())

        self.log.warning(
            '\nTrial support interval: {}; mean stride: {}\nTrials cardinality: {}\nEpisodes per trial: {}.\n'.
                format(
                self.train_range_delta,
                self.test_range_delta,
                self.total_trials,
                self.train_samples
            )
        )
        self.log.warning(
            'Trial #{} @ interval: {} <--> {}; mean row: {}'.
            format(
                self.trial_num,
                self.data.index[self.trial_mean_row - int(self.trial_range_row / 2)],
                self.data.index[self.trial_mean_row + int(self.trial_range_row / 2)],
                self.trial_mean_row
            )
        )
        self.is_ready = True

    def sample(self, **kwargs):
        """
        Randomly uniformly samples episode from `Trial` which in turn has been
        uniformly sampled from `sequence of Trials`.

        Iteratively calling this method results in::

                    - randomly draws single episode from Trial;
                - until predefined number of episodes has been drawn;
                - randomly draws Trial from Trial's distribution;
            - until bored.

        Args:
            kwargs:     not used.

        Returns:
            BTgymDataset instance containing episode data and metadata.
        """
        try:
            assert self.is_ready

        except AssertionError:
            return 'Data not ready. Call .reset() first.'

        episode = self._trial_sample_random()

        # Metadata:
        episode.metadata['type'] = False # Always `train`
        episode.metadata['trial_num'] = self.trial_num
        episode.metadata['sample_num'] = self.sample_num
        return episode

    def _trial_sample_random(self):
        if self.sample_num >= self.train_samples:
            self.trial_num += 1
            self.sample_num = 0
            self.trial_mean_row = int(self.trial_range_row / 2) +\
                                  self.trial_stride_row * int(self.total_trials * random.random())
            self.log.warning(
                'Trial #{}: from {} to {}; mean row: {}'.
                format(
                    self.trial_num,
                    self.data.index[self.trial_mean_row - int(self.trial_range_row / 2)],
                    self.data.index[self.trial_mean_row + int(self.trial_range_row / 2)],
                    self.trial_mean_row
                )
            )
        self.sample_num += 1
        self.log.debug('Trial sample #{}'.format(self.sample_num))

        return self._sample_interval(
            interval=[
                self.trial_mean_row - int(self.trial_range_row / 2),
                self.trial_mean_row + int(self.trial_range_row / 2)
            ]

        )

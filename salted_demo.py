# Copyright 2018 Scott Gorlin
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A demo implementation of a Salted Graph workflow"""

from datetime import date
from hashlib import sha256
import inspect

from luigi import DateIntervalParameter, DateParameter, FloatParameter, \
    LocalTarget, Parameter, Task, build, format
from luigi.date_interval import Week
from luigi.task import flatten
import pandas as pd
from sklearn.datasets import load_digits
from sklearn.externals import joblib
from sklearn.svm import SVC


def get_salted_version(task):
    """Create a salted id/version for this task and lineage

    :returns: a unique, deterministic hexdigest for this task
    :rtype: str
    """

    msg = ""

    # Salt with lineage
    for req in flatten(task.requires()):
        # Note that order is important and impacts the hash - if task
        # requirements are a dict, then consider doing this is sorted order
        msg += get_salted_version(req)

    # Uniquely specify this task
    msg += ','.join([

            # Hash task class source as a way of tracking versions
            # if the source code of the task changes, then the version
            # will automatically be incremented
            inspect.getsource(task.__class__)

        ] + [
            # Depending on strictness - skipping params is acceptable if
            # output already is partitioned by their params; including every
            # param may make hash *too* sensitive
            '{}={}'.format(param_name, repr(task.param_kwargs[param_name]))
            for param_name, param in sorted(task.get_params())
            if param.significant
        ]
    )
    return sha256(msg.encode()).hexdigest()


class SaltedTask(Task):
    """Lightly modified luigi task with an added salting method
    for file targets
    """
    salt_workflow = BoolParameter(
        default=True, description='Whether targets should be "salted" by preceeding tasks')
    salt_parameters = BoolParameter(
        default=False, description='Whether to also include task parameters when salting a work flow')
    salt_length = IntParameter(
        default=6, description='The number of digits of the hashed task graph to include in target names')

    def salt_target(self, target_type, file_pattern, **kwargs):
        """A target file path formed with a 'salt' kwarg.  

        Additional kwargs are passed to target_type.  The format
        process also receives self as a keyword for additional properties
        like parameters to be inserted as well.
        
        Ex.
        self.salt_target(LocalTarget, 'path/to/data-{salt}.csv', format=format.Nop)
        self.salt_target(LocalTarget, 'path/to/data-{self.date_parameter}-{salt}.csv', format=format.Nop)

        :param luigi.Target target_type: A luigi target type to use
        :param str file_pattern: A filename pattern to used with .format() to fill in blanks
        :param **kwargs: keyword args passed to target_type

        :rtype: str
       
        """
        file_path = file_pattern.format(salt=get_salted_version(self)[
                                        :self.salt_length], self=self)
        return target_type(file_path, **kwargs)


class Streams(Task):

    date = DateParameter()

    def run(self):
        # This really should be an external task, but for simplicity we'll make
        # fake data

        with self.output().open('w') as out_file:
            df = pd.DataFrame(
                {'artist':['Scott', 'Sally'],
                 'track':['Python on my mind', 'What I like about R']})

            df.to_csv(out_file, sep='\t')

    def output(self):
        return LocalTarget('data/stream/{}.tsv'.format(self.date))


class AggregateArtists(Task):

    date_interval = DateIntervalParameter()

    def output(self):
        return self.salt_target(LocalTarget,
                "data/artist_streams_{self.date_interval}-{salt}.tsv")

    def requires(self):
        return [Streams(date=date) for date in self.date_interval]

    def run(self):
        dfs = []

        for input in self.input():
            with input.open('r') as in_file:
                df = pd.read_csv(in_file, sep='\t')
                dfs.append(df.groupby('artist').size().to_frame('count'))

        together = pd.concat(dfs).reset_index().groupby('artist').sum()
        with self.output().open('w') as out_file:
            together.to_csv(out_file, sep='\t')


class SVCTask(SaltedTask):

    __version__ = '1.0'

    c = FloatParameter(default=100.)
    gamma = FloatParameter(default=1.)
    kernel = Parameter(default='rbf')


class TrainDigits(SVCTask):

    def output(self):
        return self.salt_target(LocalTarget, 'data/model-{salt}.pkl', format=format.Nop)

    def run(self):
        # http://scikit-learn.org/stable/tutorial/basic/tutorial.html
        digits = load_digits()

        svc = SVC(C=self.c, gamma=self.gamma, kernel=self.kernel)

        svc.fit(digits.data[::2], digits.target[::2])

        with self.output().open('w') as f:
            joblib.dump(svc, f, protocol=-1)


class PredictDigits(SVCTask):

    def requires(self):
        return self.clone(TrainDigits)

    def output(self):
        return self.salt_target(LocalTarget, 'data/accuracy-{salt}.txt')

    def run(self):

        with self.input().open() as f:
            svc = joblib.load(f)

        digits = load_digits()
        predictions = svc.predict(digits.data[1::2])
        with self.output().open('w') as f:
            f.write('Accuracy: {}'.format(
                (predictions == digits.target[1::2]).mean()
            ))


if __name__ == '__main__':
    agg = AggregateArtists(date_interval=Week.from_date(date(2018, 3, 7)))

    # Choose some tasks/params to run, tweak versions, etc
    build([
        agg,
        PredictDigits(),
    ], local_scheduler=True)

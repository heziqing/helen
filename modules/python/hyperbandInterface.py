from __future__ import print_function
import argparse
from hyperopt import hp
from hyperopt.pyll.stochastic import sample
import pickle
import sys
import os
import time

# Custom generator for our dataset
from modules.python.models.hyperband import Hyperband
from modules.python.TextColor import TextColor
from modules.python.models.train import train
from modules.python.Options import TrainOptions
"""
Tune hyper-parameters of a model using hyperband.
Input:
- A train CSV file
- A test CSV file

Output:
- A model with tuned hyper-parameters
"""


class WrapHyperband:
    """
    Wrap hyperband around a model to tune hyper-parameters.
    """
    # Paramters of the model
    # depth=28 widen_factor=4 drop_rate=0.0
    def __init__(self, train_file, test_file, gpu_mode, model_out_dir, log_dir, max_epochs, batch_size, num_workers):
        """
        Initialize the object
        :param train_file: A train CSV file containing train data set information
        :param test_file: A test CSV file containing train data set information
        :param gpu_mode: If true, GPU will be used to train and test the models
        :param model_out_dir: Directory to save the model
        :param log_dir: Directory to save the log
        """
        # the hyper-parameter space is defined here
        self.space = {
            # hp.loguniform returns a value drawn according to exp(uniform(low, high)) so that the logarithm of the
            # return value is uniformly distributed.
            'lr': hp.loguniform('lr', -8, -4),
            'l2': hp.loguniform('l2', -12, -4)
        }
        self.train_file = train_file
        self.test_file = test_file
        self.gpu_mode = gpu_mode
        self.log_directory = log_dir
        self.model_out_dir = model_out_dir
        self.max_epochs = max_epochs
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.hidden_size = TrainOptions.HIDDEN_SIZE
        self.gru_layers = TrainOptions.GRU_LAYERS

    def get_params(self):
        """
        Get a random draw from the parameter space.
        :return: A randomly drawn sample space
        """
        return sample(self.space)

    def try_params(self, n_iterations, model_params, model_path):
        """
        Try a parameter space to train a model with n_iterations (epochs).
        :param n_iterations: Number of epochs to train on
        :param model_params: Parameter space
        :return: trained model, optimizer and stats dictionary (loss and others)
        """
        # Number of iterations or epoch for the model to train on
        n_iterations = int(round(n_iterations))
        params, retrain_model, retrain_model_path, prev_ite = model_params
        sys.stderr.write(TextColor.BLUE + '\nEpochs: ' + str(n_iterations) + "\n" + TextColor.END)
        sys.stderr.write(TextColor.BLUE + str(params) + "\n" + TextColor.END)

        epoch_limit = int(n_iterations)
        lr = params['lr']
        l2 = params['l2']

        # train a model
        transducer_model, model_optimizer, stats_dictionary = train(self.train_file,
                                                                    self.test_file,
                                                                    self.batch_size,
                                                                    epoch_limit,
                                                                    self.gpu_mode,
                                                                    self.num_workers,
                                                                    retrain_model,
                                                                    retrain_model_path,
                                                                    self.gru_layers,
                                                                    self.hidden_size,
                                                                    lr,
                                                                    l2,
                                                                    model_dir=None,
                                                                    stats_dir=None,
                                                                    train_mode=False)

        # save_best_model(transducer_model, model_optimizer, self.hidden_size, self.gru_layers, epoch_limit, model_path)

        return transducer_model, model_optimizer, stats_dictionary

    def run(self, save_output):
        """
        Run the hyper-parameter tuning algorithm
        :param save_output: If true, output will beb saved in a pkl file
        :return:
        """
        hyperband = Hyperband(self.get_params, self.try_params, max_iteration=self.max_epochs, downsample_rate=3,
                              model_directory=self.model_out_dir, log_directory=self.log_directory)
        results = hyperband.run()

        if save_output:
            with open(self.log_directory + 'results.pkl', 'wb') as f:
                pickle.dump(results, f)

        # Print top 5 configs based on loss
        results = sorted(results, key=lambda r: r['loss'])[:5]
        for i, result in enumerate(results):
            print(i+1)
            print(result)
            print("Loss:\t\t", result['loss'])
            print("Accuracy:\t", result['accuracy'])
            print("Params:\t\t", result['params'])
            print("Model path:\t", result['model_path'])
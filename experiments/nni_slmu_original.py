import os
import nengo
from nengo.utils.filter_design import cont2discrete
import nengo_dl
import numpy as np
import pandas as pd
import tensorflow as tf
from keras.callbacks import TensorBoard, Callback
import argparse
import logging
import json
import nni
from sklearn.preprocessing import MinMaxScaler
from scipy.signal import butter, freqz
from nni.tuner import Tuner
from nni.experiment import Experiment
from nni.algorithms.hpo.hyperopt_tuner import HyperoptTuner
from nni.tools.nnictl import updater, nnictl_utils


os.environ["CUDA_VISIBLE_DEVICES"] = '0'  
os.environ["TF_FORCE_GPU_ALLOW_GROWTH"] = 'true'  

seed = 0
os.environ['PYTHONHASHSEED'] = str(seed)
tf.random.set_seed(seed)
np.random.seed(seed)
rng = np.random.RandomState(seed)


### SET NETWORK TYPE:
network_type = 'slmu'


##### DATASET-RELATED SETTINGS #######################

# name to identify experiments on different datasets
dataset_name = 'wisdm2'

# file name of the dataset
datafile = 'data_watch_40'

######################################################

searchspace_path = '../searchspaces/nni_SearchSpace_'+network_type+'.json'

eps = 10


def load_dataset(file_name):
    filepath = os.path.join('../data/',file_name+'.npz')
    a = np.load(filepath)
    return (a['arr_0'], a['arr_1'], a['arr_2'], a['arr_3'], a['arr_4'], a['arr_5'])


class DeviceData:
    def __init__(self, sample, fs, channels):
        self.data = []
        sample = sample.T
        for data_axis in range(sample.shape[0]):
            self.data.append(sample[data_axis, :])

        self.fs = fs
        self.freq_range = (0.5, np.floor(self.fs / 2))

        freq_min, freq_max = self.freq_range
        octave = (channels - 0.5) * np.log10(2) / np.log10(freq_max / freq_min)
        self.freq_centr = np.array([freq_min * (2 ** (ch / octave)) for ch in range(channels)])
        self.freq_poli = np.array(
            [(freq * (2 ** (-1 / (2 * octave))), (freq * (2 ** (1 / (2 * octave))))) for freq in self.freq_centr])
        self.freq_poli[-1, 1] = fs / 2 * 0.99999

    def decomposition(self, filterbank):
        self.components = []
        for data_axis in self.data:
            tmp = []
            for num, den in filterbank:
                from scipy.signal import lfilter
                tmp.append(lfilter(num, den, data_axis))
            self.components.append(tmp)


def frequency_decomposition(array, channels=5, fs=20, order=2):

    array_dec = []

    for ii in range(len(array)):
    
        sample = DeviceData(array[ii], fs, channels)
    
        butter_filterbank = []
        for fl, fh in sample.freq_poli:
            num, den = butter(N=order, Wn=(fl, fh), btype='band', fs=sample.fs)
            butter_filterbank.append([num, den])
    
        sample.decomposition(butter_filterbank)
    
        features = []
        for data_axis in sample.components:
            for component in data_axis:
                features.append(np.array(component))
        features = np.vstack(features)
        features = features.T
    
        array_dec.append(features)

    return np.array(array_dec)


class SendMetrics(Callback):
    '''
    Keras callback to send metrics to NNI framework
    '''
    def on_epoch_end(self, epoch, logs={}):
        '''
        Run on end of each epoch
        '''
        LOG.debug(logs)
        nni.report_intermediate_result(logs['val_probe_accuracy']*100)


def report_result(result, result_type):

    if result_type == 'test':
        report_file = out_dir + 'nni_' + network_type + '_' + nni.get_experiment_id() + '_' + result_type + '_accs'
    else:
        report_file = out_dir + 'nni_' + network_type + '_' + nni.get_experiment_id() + '_' + result_type + '_accs_' + nni.get_trial_id()
    
    with open(report_file, 'a') as f:
        f.write(str(result))
        f.write('\n')
    
    return report_file


def run_LMU(args, params):

    (x_train, x_val, x_test, y_train_oh, y_val_oh, y_test_oh) = load_dataset(datafile)

    if args.freq_dec:
        x_train = frequency_decomposition(x_train)
        x_val = frequency_decomposition(x_val)
        x_test = frequency_decomposition(x_test)
        LOG.debug(print("Input signals are decomposed in frequency"))
        LOG.debug(print('x_train shape: '+str(x_train.shape)))
        LOG.debug(print('x_val shape: '+str(x_val.shape)))
        LOG.debug(print('x_test shape: '+str(x_test.shape)))

    timesteps = len(x_train[0])
    input_dim = len(x_train[0][0])
    n_classes = len(y_train_oh[0])

    y_train = np.argmax(y_train_oh, axis=-1)
    y_val = np.argmax(y_val_oh, axis=-1)
    y_test = np.argmax(y_test_oh, axis=-1)

    y_train = y_train[:, None, None]
    y_test = y_test[:, None, None]
    y_val = y_val[:, None, None]

    with nengo.Network(seed=seed) as net:
        # remove some unnecessary features to speed up the training
        nengo_dl.configure_settings(
            trainable=None,
            stateful=False,
            keep_history=False,
        )

        # input node
        inp = nengo.Node(np.zeros(input_dim))
    
        order = int(params['order'])
        theta = params['theta']
        input_d = input_dim
        tau = params['tau']
    
        Q = np.arange(order, dtype=np.float64)
        R = (2 * Q + 1)[:, None] / theta
        j, i = np.meshgrid(Q, Q)
        A = np.where(i < j, -1, (-1.0) ** (i - j + 1)) * R 
        B = (-1.0) ** Q[:, None] * R 
        C = np.ones((1, order))
        D = np.zeros((1,))

        disc_step = 1/theta
        A, B, _, _, _ = cont2discrete((A, B, C, D), dt=disc_step, method="zoh")
    
        A_H = 1/(1-np.exp(-disc_step/tau)) * (A - np.exp(-disc_step/tau)*np.identity(order))
        B_H = 1/(1-np.exp(-disc_step/tau)) * B

        for conn in net.all_connections:
            conn.synapse = params['synapse_all']

        max_rate = params['max_rate']
        amplitude = 1/max_rate
        lmu_inner = nengo.networks.EnsembleArray(n_neurons=int(params['n_neurons']),
                                                 n_ensembles=order, 
                                                 neuron_type=nengo.SpikingRectifiedLinear(amplitude=amplitude),
                                                 max_rates=nengo.dists.Choice([max_rate]))
        conn_inner = nengo.Connection(lmu_inner.output, lmu_inner.input, transform=A_H, synapse=tau)
        net.config[conn_inner].trainable = True
    
        conn_in = nengo.Connection(inp, lmu_inner.input, transform=np.ones((1, input_d))*B_H, synapse=params['synapse_in'])
        net.config[conn_in].trainable = True
    
        # dense linear readout
        out = nengo.Node(size_in=n_classes)
        conn_out = nengo.Connection(lmu_inner.output, out, transform=nengo_dl.dists.Glorot(), synapse=params['synapse_out'])
        net.config[conn_out].trainable = True

        # record output
        p = nengo.Probe(out)
    
        
    with nengo_dl.Simulator(net, minibatch_size=params['minibatch']) as sim:

        lmu_model_summary = sim.keras_model
        lmu_params = sum(np.prod(s.shape) for s in lmu_model_summary.weights)
        lmu_trainable_params = sum(np.prod(w.shape) for w in lmu_model_summary.trainable_weights)
        LOG.debug(print('Total params:','{:,d}'.format(lmu_params)))
        LOG.debug(print('Trainable params:','{:,d}'.format(lmu_trainable_params)))
        LOG.debug(print('Non-trainable params:','{:,d}'.format(lmu_params-lmu_trainable_params)))
        
        sim.compile(
            loss=tf.losses.SparseCategoricalCrossentropy(from_logits=True),
            optimizer=tf.optimizers.Adam(params['lr']),
            metrics=["accuracy"],
        )

        class CheckPoint(Callback):
            '''
            Keras callback to check training results epoch by epoch
            '''
            def on_epoch_end(self, epoch, logs={}):
                '''
                Run on end of each epoch
                '''
                report_file = report_result(logs['val_probe_accuracy']*100,'validation')

                with open(report_file, 'r') as f:
                    if logs['val_probe_accuracy']*100 >= np.max(np.asarray([(line.strip()) for line in f], dtype=np.float64)):
                        sim.save_params(out_dir+'best_train_'+nni.get_experiment_id()+'_'+nni.get_trial_id())

        history = sim.fit(x_train, y_train, 
                          validation_data = (x_val, y_val),
                          epochs=args.epochs,
                          callbacks=[SendMetrics(), CheckPoint(), TensorBoard(log_dir=TENSORBOARD_DIR)])

        sim.load_params(out_dir+'best_train_'+nni.get_experiment_id()+'_'+nni.get_trial_id())
        test_accuracy = sim.evaluate(x_test, y_test, verbose=True)["probe_accuracy"]
        report_file = report_result(test_accuracy*100, 'test')
        with open(report_file, 'r') as f:
            if test_accuracy*100 >= np.max(np.asarray([(line.strip()) for line in f], dtype=np.float64)):
                sim.save_params(out_dir+'best_test_'+nni.get_experiment_id())

        LOG.debug(print(f"Final validation accuracy: {100 * history.history['val_probe_accuracy'][-1]:.2f}%"))
        LOG.debug(print(f"Best validation accuracy: {100 * np.max(history.history['val_probe_accuracy']):.2f}%"))
        LOG.debug(print(f"Test accuracy from training with best validation accuracy: {100 * test_accuracy:.2f}%"))
        nni.report_final_result(test_accuracy*100)
    
    sim.close()

    os.remove(out_dir+'best_train_'+nni.get_experiment_id()+'_'+nni.get_trial_id()+'.npz')
    os.remove(out_dir+'nni_'+network_type+'_'+nni.get_experiment_id()+'_validation_accs_'+nni.get_trial_id())


if __name__ == '__main__':
    PARSER = argparse.ArgumentParser()
    PARSER.add_argument("--epochs", type=int, default=eps, help="Train epochs", required=False)
    PARSER.add_argument("--filename", type=str, default=searchspace_path, help="File name for search space", required=False)
    PARSER.add_argument("--id", type=str, default=nni.get_experiment_id(), help="Experiment ID", required=False)
    PARSER.add_argument("--freq_dec", type=bool, default=False, help="Frequency decomposition of input signals", required=False)
    
    ARGS, UNKNOWN = PARSER.parse_known_args()

    LOG = logging.getLogger('MLinApp_'+network_type+'_'+dataset_name)
    out_dir = '../output/tmp_' + network_type + '_' + nni.get_experiment_id() + '_' + dataset_name + '/'
    os.environ['NNI_OUTPUT_DIR'] = out_dir
    TENSORBOARD_DIR = os.environ['NNI_OUTPUT_DIR']
    
    try:
        
        n_tr = 200
        if (nni.get_sequence_id() > 0) & (nni.get_sequence_id()%n_tr == 0):
            updater.update_searchspace(ARGS) # it will use ARGS.filename to update the search space
        
        PARAMS = nni.get_next_parameter()
        LOG.debug(PARAMS)
        
        run_LMU(ARGS, PARAMS)
    
    except Exception as e:
        LOG.exception(e)
        raise
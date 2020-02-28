import os
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel
import sys
import torch
import torch.nn as nn
import time
from torch.utils.data import DataLoader
from modules.python.models.dataloader_predict import SequenceDataset
from modules.python.TextColor import TextColor
from modules.python.models.ModelHander import ModelHandler
from modules.python.Options import ImageSizeOptions, TrainOptions
from modules.python.DataStore import DataStore
"""
This script implements the predict method that is used by the call consensus method.

The algorithm is described here:

  1) INPUTS:
    - directory path to the image files generated by MarginPolish
    - model path directing to a trained model
    - batch size for minibatch prediction
    - num workers for minibatch processing threads
    - output directory path to where the output hdf5 will be saved
  2) METHOD:
    - Call predict function that loads the neural network and generates base predictions and saves it into a hdf5 file
        - Loads the model
        - Iterates over the input images in minibatch
        - For each image uses a sliding window method to slide of the image sequence
        - Aggregate the predictions to get sequence prediction for the entire image sequence
        - Save all the predictions to a file
  3) OUTPUT:
    - A hdf5 file containing all the base predictions   
"""


def predict(test_file, output_filename, model_path, batch_size, num_workers, rank, device_id):
    """
    The predict method loads images generated by MarginPolish and produces base predictions using a
    sequence transduction model based deep neural network. This method loads the model and iterates over
    minibatch images to generate the predictions and saves the predictions to a hdf5 file.

    :param test_file: File to predict on
    :param output_filename: Name and path to the output file
    :param batch_size: Batch size used for minibatch prediction
    :param model_path: Path to a trained model
    :param rank: Rank of this caller
    :param num_workers: Number of workers to be used by the dataloader
    :param device_id: Device id of the model to set to
    :param threads: Number of threads to use with pytorch
    :return: Prediction dictionary
    """
    # create the output hdf5 file where all the predictions will be saved
    prediction_data_file = DataStore(output_filename + "_" + str(rank) + ".hdf", mode='w')

    # load the model using the model path
    transducer_model, hidden_size, gru_layers, prev_ite = \
        ModelHandler.load_simple_model(model_path,
                                       input_channels=ImageSizeOptions.IMAGE_CHANNELS,
                                       image_features=ImageSizeOptions.IMAGE_HEIGHT,
                                       seq_len=ImageSizeOptions.SEQ_LENGTH,
                                       num_base_classes=ImageSizeOptions.TOTAL_BASE_LABELS,
                                       num_rle_classes=ImageSizeOptions.TOTAL_RLE_LABELS)
    transducer_model.eval()

    torch.cuda.set_device(device_id)
    transducer_model.to(device_id)
    transducer_model.eval()
    transducer_model = DistributedDataParallel(transducer_model, device_ids=[device_id])

    # only output for rank 0 caller
    if rank == 0:
        print(output_filename + "_" + str(rank) + ".hdf")
        sys.stderr.write(TextColor.GREEN + 'INFO: TORCH THREADS SET TO: ' + str(torch.get_num_threads()) + ".\n"
                         + TextColor.END)
        # notify that the process has started and loading data
        sys.stderr.write(TextColor.PURPLE + 'Loading data\n' + TextColor.END)

    # create a pytorch dataset and dataloader that loads the data in mini_batches
    test_data = SequenceDataset(image_directory=None, file_list=test_file)
    test_loader = DataLoader(test_data,
                             batch_size=batch_size,
                             shuffle=False,
                             num_workers=num_workers)

    # iterate over the data in minibatches
    with torch.no_grad():
        # keep an eye for batch
        total_batches = len(test_loader)
        batch_iterator = 0

        # the dataloader loop, iterates in minibatches. tqdm is the progress logger.
        for contig, contig_start, contig_end, chunk_id, images, position, filename in test_loader:
            start_time = time.time()
            # the images are usually in uint8, convert them to FloatTensor
            images = images.type(torch.FloatTensor)
            # initialize the first hidden input as all zeros
            hidden = torch.zeros(images.size(0), 2 * TrainOptions.GRU_LAYERS, TrainOptions.HIDDEN_SIZE)

            # this is a multi-task neural network where we predict a base and a run-length. We use two dictionaries
            # to keep track of predictions.
            # these two dictionaries save predictions for each of the chunks and later we aggregate all the predictions
            # over the entire sequence to get a sequence prediction for the whole sequence.
            prediction_base_tensor = torch.zeros((images.size(0), images.size(1), ImageSizeOptions.TOTAL_BASE_LABELS))
            prediction_rle_tensor = torch.zeros((images.size(0), images.size(1), ImageSizeOptions.TOTAL_RLE_LABELS))

            # move the tensors to CUDA
            prediction_base_tensor = prediction_base_tensor.to(device_id)
            prediction_rle_tensor = prediction_rle_tensor.to(device_id)

            # now the images usually contain 1000 bases, we iterate on a sliding window basis where we process
            # the window size then jump to the next window
            for i in range(0, ImageSizeOptions.SEQ_LENGTH, TrainOptions.WINDOW_JUMP):
                # if current position + window size goes beyond the size of the window, that means we've reached the end
                if i + TrainOptions.TRAIN_WINDOW > ImageSizeOptions.SEQ_LENGTH:
                    break
                chunk_start = i
                chunk_end = i + TrainOptions.TRAIN_WINDOW

                # get the image chunk
                image_chunk = images[:, chunk_start:chunk_end]

                # move the images and the hidden layers to CUDA
                images = images.to(device_id)
                hidden = hidden.to(device_id)

                # run inference
                output_base, output_rle, hidden = transducer_model(image_chunk, hidden)

                # now calculate how much padding is on the top and bottom of this chunk so we can do a simple
                # add operation
                top_zeros = chunk_start
                bottom_zeros = ImageSizeOptions.SEQ_LENGTH - chunk_end

                # we run a softmax a padding to make the output tensor compatible for adding
                inference_layers = nn.Sequential(
                    nn.Softmax(dim=2),
                    nn.ZeroPad2d((0, 0, top_zeros, bottom_zeros))
                )
                inference_layers = inference_layers.to(device_id)

                # run the softmax and padding layers
                base_prediction = inference_layers(output_base)
                rle_prediction = inference_layers(output_rle)

                # now simply add the tensor to the global counter
                prediction_base_tensor = torch.add(prediction_base_tensor, base_prediction)
                prediction_rle_tensor = torch.add(prediction_rle_tensor, rle_prediction)

            # all done now create a SEQ_LENGTH long prediction list
            prediction_base_tensor = prediction_base_tensor.cpu()
            prediction_rle_tensor = prediction_rle_tensor.cpu()

            base_values, base_labels = torch.max(prediction_base_tensor, 2)
            rle_values, rle_labels = torch.max(prediction_rle_tensor, 2)

            predicted_base_labels = base_labels.cpu().numpy()
            predicted_rle_labels = rle_labels.cpu().numpy()

            batch_iterator += 1

            if rank == 0:
                # calculate the expected time to finish
                eta = (time.time() - start_time) * (total_batches - batch_iterator)
                hours = str(int(eta/3600))
                eta = eta - (eta/3600)
                mins = str(int(eta/60))
                secs = str(int(eta) % 60)
                time_stamp = hours + " HOURS " + mins + " MINS " + secs + " SECS."
                batch_string = "BATCHES DONE: " + str(batch_iterator) + "/" + str(total_batches) + ". "
                time_left = "ESTIMATED TIME LEFT: " + str(time_stamp)
                sys.stderr.write(TextColor.GREEN + "INFO: " + batch_string + time_left + "\n" + TextColor.END)

            # go to each of the images and save the predictions to the file
            for i in range(images.size(0)):
                prediction_data_file.write_prediction(contig[i], contig_start[i], contig_end[i], chunk_id[i],
                                                      position[i], predicted_base_labels[i], predicted_rle_labels[i],
                                                      filename[i])


def cleanup():
    dist.destroy_process_group()


def setup(rank, total_callers, args, all_input_files, all_devices):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'

    # initialize the process group
    dist.init_process_group("gloo", rank=rank, world_size=total_callers)

    # expand the arguments
    output_filepath, model_path, batch_size, num_workers = args

    # call prediction function
    predict(all_input_files[rank],
            output_filepath,
            model_path,
            batch_size,
            num_workers,
            rank,
            all_devices[rank])
    cleanup()


def predict_gpu(file_chunks, output_filepath, model_path, batch_size, total_callers, devices, num_workers):
    """
    Create a prediction table/dictionary of an images set using a trained model.
    :param file_chunks: Path to chunked files
    :param batch_size: Batch size used for prediction
    :param model_path: Path to a trained model
    :param output_filepath: Path to output directory
    :param total_callers: Number of callers to spawn
    :param devices: List of available CUDA devices
    :param num_workers: Number of workers to be used by the dataloader
    :return: Prediction dictionary
    """
    # create the arguments to send for prediction
    args = (output_filepath, model_path, batch_size, num_workers)

    # spawn the processes to call the prediction method
    mp.spawn(setup,
             args=(total_callers, args, file_chunks, devices),
             nprocs=total_callers,
             join=True)

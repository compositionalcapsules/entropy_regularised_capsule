import torch

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
LR = 0.0001
BATCH_SIZE = 128
NUMBER_OF_TRIALS = 3
SMALL_NORB_PATH = '../../data/'

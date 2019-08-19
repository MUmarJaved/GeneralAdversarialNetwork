import argparse
import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader
from os import makedirs
from os.path import join, exists
from IPython.core.debugger import Pdb

# from preprocess import preprocess
from dataset import ReviewsDataset
from train import train_model, test_model
from model import HAN
from utils import log

parser = argparse.ArgumentParser()
parser.add_argument('--config', type=str, default='config.yaml')
parser.add_argument('--testfile', type=str, metavar='PATH')
parser.add_argument('--outputfile', type=str, metavar='PATH')


def load_datasets(config, phases, logfile=None):
    config = config['data']
    log('Loading vocabularies...', logfile)
    import pickle
    review_vocab = pickle.load(open(join(config['dir'], config['review_vocab']), 'rb'))
    if config['review_vocab'] != config['summary_vocab']:
        summary_vocab = pickle.load(open(join(config['dir'], config['summary_vocab']), 'rb'))
    else:
        summary_vocab = review_vocab

    log('Loading preprocessed datasets...', logfile)
    datafiles = {x: config[x]['jsonfile'] for x in phases}
    datasets = {x: ReviewsDataset(data=join(config['dir'], datafiles[x]), review_vocab=review_vocab, summary_vocab=summary_vocab)
                for x in phases}

    def collate_fn(batch):
        reviews = [sample[0] for sample in batch]
        summaries = [sample[1] for sample in batch]
        targets = torch.LongTensor([sample[2] for sample in batch])
        return (reviews, summaries, targets)

    if 'weights' not in config or not config['weights']:
        dataloaders = {x: DataLoader(datasets[x], batch_size=config[x]['batch_size'], shuffle=True if x == 'train' else False, collate_fn=collate_fn) for x in phases}
    else:
        if config['weights'] == 'weighted':
            samplers = {x: datasets[x].get_sampler() if x == 'train' else None for x in phases}
        else:
            samplers = {x: datasets[x].get_sampler(np.array(config['weights'])) if x == 'train' else None for x in phases}
        dataloaders = {x: DataLoader(datasets[x], batch_size=config[x]['batch_size'], shuffle=False, sampler=samplers[x], collate_fn=collate_fn) for x in phases}

    dataset_sizes = {x: len(datasets[x]) for x in phases}
    log(dataset_sizes, logfile)
    log("review vocab size: {}".format(len(review_vocab.itos)), logfile)
    log("summary vocab size: {}".format(len(summary_vocab.itos)), logfile)
    return dataloaders, review_vocab, summary_vocab


def build_model(config, review_vocab, summary_vocab, logfile=None):
    use_gpu = config['use_gpu']
    # Create Model
    config['model']['params']['review_vocab_size'] = len(review_vocab)
    config['model']['params']['summary_vocab_size'] = len(summary_vocab)
    config['model']['params']['use_gpu'] = use_gpu
    config = config['model']
    model = HAN(**config['params'])
    log(model, logfile)
    # Copy pretrained word embeddings
    model.review_lookup.weight.data.copy_(review_vocab.vectors)
    if 'combined_lookup' not in config['params']:
        config['params']['combined_lookup'] = False
    if config['params']['use_summary'] and not config['params']['combined_lookup']:
        model.summary_lookup.weight.data.copy_(summary_vocab.vectors)
    if use_gpu:
        model = model.cuda()
    return model


def reload(config, model, optimizer=None, logfile=None):
    save_dir = config['save_dir']
    config = config['model']
    best_fscore = 0
    start_epoch = 0
    if 'reload' in config:
        reload_path = join(save_dir, config['reload'])
        if exists(reload_path):
            log("=> loading checkpoint/model found at '{0}'".format(reload_path), logfile)
            checkpoint = torch.load(reload_path)
            if model.__version__() != checkpoint['model_version']:
                log('Model version mismatch: current version={}, checkpoint version={}'
                    .format(model.__version__(), checkpoint['model_version']))
            start_epoch = checkpoint['epoch']
            best_fscore = checkpoint['fscore']
            model.load_state_dict(checkpoint['state_dict'])
            if optimizer is not None:
                optimizer.load_state_dict(checkpoint['optimizer'])
        else:
            log("no checkpoint/model found at '{0}'".format(reload_path), logfile)
    return model, optimizer, best_fscore, start_epoch


def main(config):
    logfile = join(config['save_dir'], 'log')
    log(config, logfile)
    if config['mode'] == 'test':
        phases = ['test']
    else:
        phases = ['train', 'val']
    dataloaders, review_vocab, summary_vocab = load_datasets(config, phases, logfile)

    # Create Model
    model = build_model(config, review_vocab, summary_vocab, logfile)

    if config['mode'] == 'train':
        # Select Optimizer
        if config['optim']['class'] == 'sgd':
            optimizer = optim.SGD(filter(lambda p: p.requires_grad, model.parameters()),
                                  **config['optim']['params'])
        elif config['optim']['class'] == 'rmsprop':
            optimizer = optim.RMSprop(filter(lambda p: p.requires_grad, model.parameters()),
                                      **config['optim']['params'])
        else:
            optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()),
                                   **config['optim']['params'])
        # Reload model from checkpoint if provided
        model, optimizer, best_fscore, start_epoch = reload(config, model, optimizer, logfile)
        log(optimizer, logfile)
        criterion = nn.CrossEntropyLoss()
        patience = config['optim']['scheduler']['patience']
        factor = config['optim']['scheduler']['factor']
        scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', patience=patience, factor=factor,
                                                   threshold=0.05, threshold_mode='rel', verbose=True)
        log(scheduler, logfile)
        log("Begin Training...", logfile)
        model = train_model(model, dataloaders, criterion, optimizer, scheduler, config['save_dir'],
                            num_epochs=config['training']['n_epochs'], use_gpu=config['use_gpu'],
                            best_fscore=best_fscore, start_epoch=start_epoch, logfile=logfile)
    elif config['mode'] == 'test':
            # Reload model from checkpoint if provided
            model, _, _, _ = reload(config, model, logfile=logfile)
            log('Testing on {}...'.format(config['data']['test']['jsonfile']))
            test_model(model, dataloaders['test'], config['outputfile'], use_gpu=config['use_gpu'], logfile=logfile)
    else:
        log("Invalid config mode %s !!" % config['mode'], logfile)


if __name__ == '__main__':
    global args
    args = parser.parse_args()
    import yaml
    config = yaml.load(open(args.config))
    config['use_gpu'] = config['use_gpu'] and torch.cuda.is_available()
    # TODO: seeding still not perfect
    torch.manual_seed(config['seed'])
    torch.cuda.manual_seed(config['seed'])
    if args.testfile:
        config['data']['test']['jsonfile'] = args.testfile
        config['outputfile'] = args.outputfile
        config['data']['dir'] = ''
        config['save_dir'] = ''
    else:
        makedirs(config['save_dir'], exist_ok=True)
    main(config)

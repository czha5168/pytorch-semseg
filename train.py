DEBUG=False
def log(s):
    if DEBUG:
        print(s)
##################
def display(string):
    print(string)
    logger.info(string)
###################
import math
from glob import glob
import random
def init_data_split(root):
    # init_train_val_split
    file_paths = glob(root + '*_lesion*.nhdr')
    ratio = 0.3
    case_index = [path.split('/')[-1].split('_lesion')[0] for path in file_paths]
    random.shuffle(case_index)
    case_index_UNC = [temp for temp in case_index if 'UNC' in temp]
    case_index_CHB = [temp for temp in case_index if 'CHB' in temp]
    random.shuffle(case_index_CHB)
    random.shuffle(case_index_UNC)
    val_case_index_UNC = case_index_UNC[:int(ratio * (len(case_index_UNC)))]
    val_case_index_CHB = case_index_CHB[:int(ratio * (len(case_index_CHB)))]
    val_case_index = []
    val_case_index.extend(val_case_index_CHB)
    val_case_index.extend(val_case_index_UNC)
    return {'file_paths': file_paths, 'val_case_index': val_case_index, 'case_index':case_index}
###################
def prep_class_weights(ratio):
    weight_foreback = torch.ones(2)
    weight_foreback[0] = 1 / (1 - ratio)
    weight_foreback[1] = 1 / ratio
    weight_foreback = weight_foreback.cuda()
    display("CE's Weight:{}".format(weight_foreback))
    return weight_foreback
###################
import os
import subprocess
import sys
import yaml
import time
import shutil
import torch
import visdom
import random
import argparse
import datetime
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
from torchvision.utils import make_grid
from torch.utils import data

from ptsemseg.models import get_model
from ptsemseg.loss import get_loss_function
from ptsemseg.loader import get_loader
from ptsemseg.utils import get_logger
from ptsemseg.metrics import runningScore, averageMeter
from ptsemseg.augmentations import get_composed_augmentations
from ptsemseg.schedulers import get_scheduler
from ptsemseg.optimizers import get_optimizer

from tensorboardX import SummaryWriter

def train(cfg, writer, logger):
    # Setup dataset split before setting up the seed for random
    split_info = init_data_split(cfg['data']['path'])  # miccai2008 dataset

    # Setup seeds
    torch.manual_seed(cfg.get('seed', 1337))
    torch.cuda.manual_seed(cfg.get('seed', 1337))
    np.random.seed(cfg.get('seed', 1337))
    random.seed(cfg.get('seed', 1337))

    # Setup device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Setup Cross Entropy Weight
    weight = prep_class_weights(cfg['training']['cross_entropy_ratio'])

    # Setup Augmentations
    augmentations = cfg['training'].get('augmentations', None)
    log(('augmentations_cfg:', augmentations))


    data_aug = get_composed_augmentations(augmentations)

    # Setup Dataloader
    data_loader = get_loader(cfg['data']['dataset'])
    data_path = cfg['data']['path']
    t_loader = data_loader(
        data_path,
        is_transform=True,
        split=cfg['data']['train_split'],
        img_size=(cfg['data']['img_rows'], cfg['data']['img_cols']),
        augmentations=data_aug, split_info = split_info, patch_size = cfg['training']['patch_size'])

    v_loader = data_loader(
        data_path,
        is_transform=True,
        split=cfg['data']['val_split'],
        img_size=(cfg['data']['img_rows'], cfg['data']['img_cols']), split_info = split_info, patch_size = cfg['training']['patch_size'])

    n_classes = t_loader.n_classes
    trainloader = data.DataLoader(t_loader,
                                  batch_size=cfg['training']['batch_size'], 
                                  num_workers=cfg['training']['n_workers'], 
                                  shuffle=False)

    valloader = data.DataLoader(v_loader, 
                                batch_size=cfg['training']['batch_size'], 
                                num_workers=cfg['training']['n_workers'])

    # Setup Metrics
    running_metrics_val = runningScore(n_classes)

    # Setup Model
    model = get_model(cfg['model'], n_classes).to(device)

    model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))

    # Setup optimizer, lr_scheduler and loss function
    optimizer_cls = get_optimizer(cfg)
    optimizer_params = {k:v for k, v in cfg['training']['optimizer'].items() 
                        if k != 'name'}

    optimizer = optimizer_cls(model.parameters(), **optimizer_params)
    logger.info("Using optimizer {}".format(optimizer))

    scheduler = get_scheduler(optimizer, cfg['training']['lr_schedule'])

    loss_fn = get_loss_function(cfg)
    logger.info("Using loss {}".format(loss_fn))

    start_iter = 0
    if cfg['training']['resume'] is not None:
        if os.path.isfile(cfg['training']['resume']):
            logger.info(
                "Loading model and optimizer from checkpoint '{}'".format(cfg['training']['resume'])
            )
            checkpoint = torch.load(cfg['training']['resume'])
            model.load_state_dict(checkpoint["model_state"])
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            scheduler.load_state_dict(checkpoint["scheduler_state"])
            start_iter = checkpoint["epoch"]
            logger.info(
                "Loaded checkpoint '{}' (iter {})".format(
                    cfg['training']['resume'], checkpoint["epoch"]
                )
            )
        else:
            logger.info("No checkpoint found at '{}'".format(cfg['training']['resume']))

    val_loss_meter = averageMeter()
    time_meter = averageMeter()

    best_iou = -100.0
    i_train_iter = start_iter
    flag = True

    display('Training from {}th iteration'.format(i_train_iter))
    while i_train_iter <= cfg['training']['train_iters'] and flag:
        i_batch_idx = 0
        train_iter_start_time = time.time()
        for (images, labels, case_index_list) in trainloader:
            start_ts_network = time.time()
            scheduler.step()
            model.train()
            images = images.to(device)
            labels = labels.to(device)

            optimizer.zero_grad()
            outputs = model(images)
            log('TrainIter=> images.size():{} labels.size():{} | outputs.size():{}'.format(images.size(), labels.size(), outputs.size()))
            loss = loss_fn(input=outputs, target=labels, weight=weight, size_average=cfg['training']['loss']['size_average'])

            loss.backward()
            optimizer.step()
            
            time_meter.update(time.time() - start_ts_network)

            print_per_batch_check = True if cfg['training']['print_interval_per_batch'] else i_batch_idx+1 == len(trainloader)
            if (i_train_iter + 1) % cfg['training']['print_interval'] == 0 and print_per_batch_check:
                fmt_str = "Iter [{:d}/{:d}::{:d}/{:d}]  Loss: {:.4f}  NetworkTime/Image: {:.4f}"
                print_str = fmt_str.format(i_train_iter + 1,
                                           cfg['training']['train_iters'],
                                           i_batch_idx+1, len(trainloader),
                                           loss.item(),
                                           time_meter.avg / cfg['training']['batch_size'])

                display(print_str)
                writer.add_scalar('loss/train_loss', loss.item(), i_train_iter+1)
                time_meter.reset()
            i_batch_idx += 1
        entire_time_all_cases = time.time()-train_iter_start_time
        display('EntireTime for {}th training iteration: {:.4f}   EntireTime/Image: {:.4f}'.format(i_train_iter+1,
                                                                                                 entire_time_all_cases,
                                                                                                 entire_time_all_cases/(len(trainloader)*cfg['training']['batch_size'])))

        if (i_train_iter + 1) % cfg['training']['val_interval'] == 0 or \
           (i_train_iter + 1) == cfg['training']['train_iters']:
            model.eval()
            with torch.no_grad():
                for i_val, (images_val, labels_val, case_index_list_val) in enumerate(valloader):
                    images_val = images_val.to(device)
                    labels_val = labels_val.to(device)

                    outputs_val = model(images_val)
                    log('ValIter=> images_val.size():{} labels_val.size():{} | outputs.size():{}'.format(images_val.size(),
                                                                                                         labels_val.size(),
                                                                                                   outputs_val.size()))

                    val_loss = loss_fn(input=outputs_val, target=labels_val, weight=weight, size_average=cfg['training']['loss']['size_average'])
                    pred = outputs_val.data.max(1)[1].cpu().numpy()
                    gt = labels_val.data.cpu().numpy()


                    running_metrics_val.update(gt, pred)
                    val_loss_meter.update(val_loss.item())
                    #torch.Size([1, 3, 160, 160, 160])torch.Size([1, 160, 160, 160])torch.Size([1, 2, 160, 160, 160])
                    #print(images_val.size(), labels_val.size(), outputs_val.size())

                    '''
                        This FOR-LOOP is used to visualize validation data via tensorboard
                        It would take 3s roughly.
                    '''
                    for batch_identifier_index, case_index in enumerate(case_index_list_val):
                        tensor_grid = []
                        image_val = images_val[batch_identifier_index, :, :, :, :].float()
                        label_val = labels_val[batch_identifier_index, :, :, :].float()
                        output_val = images_val[batch_identifier_index, 1, :, :, :].float()
                        #torch.Size([3, 160, 160, 160]) torch.Size([160, 160, 160]) torch.Size([160, 160, 160])
                        #print(image_val.size(), label_val.size(), output_val.size())
                        for z_index in range(images_val.size()[-1]):
                            label_slice = label_val[:, :, z_index]
                            output_slice = output_val[:, :, z_index]
                            if label_slice.sum() == 0 and output_slice.sum() == 0:
                                continue
                            image_slice = image_val[:, :, :, z_index]
                            label_slice = label_slice.unsqueeze_(0).repeat(3, 1, 1)
                            output_slice = output_slice.unsqueeze_(0).repeat(3, 1, 1)
                            slice_list = [image_slice,output_slice, label_slice]
                            slice_grid = make_grid(slice_list, padding=20)
                            tensor_grid.append(slice_grid)
                        tensorboard_image_tensor = make_grid(tensor_grid, nrow=int(math.sqrt(len(tensor_grid)/3))+1, padding=0).permute(1, 2, 0).cpu().numpy()
                        #print(tensorboard_image_tensor.shape, type(tensorboard_image_tensor))
                        writer.add_image(case_index, tensorboard_image_tensor, i_train_iter)
            writer.add_scalar('loss/val_loss', val_loss_meter.avg, i_train_iter+1)
            logger.info("Iter %d Loss: %.4f" % (i_train_iter + 1, val_loss_meter.avg))

            score, class_iou = running_metrics_val.get_scores()
            for k, v in score.items():
                print(k, v)
                logger.info('{}: {}'.format(k, v))
                writer.add_scalar('val_metrics/{}'.format(k), v, i_train_iter+1)

            for k, v in class_iou.items():
                logger.info('{}: {}'.format(k, v))
                writer.add_scalar('val_metrics/cls_{}'.format(k), v, i_train_iter+1)

            val_loss_meter.reset()
            running_metrics_val.reset()

            if score["Mean IoU : \t"] >= best_iou:
                best_iou = score["Mean IoU : \t"]
                state = {
                    "epoch": i_train_iter + 1,
                    "model_state": model.state_dict(),
                    "optimizer_state": optimizer.state_dict(),
                    "scheduler_state": scheduler.state_dict(),
                    "best_iou": best_iou,
                }
                save_path = os.path.join(writer.file_writer.get_logdir(),
                                         "{}_{}_best_model.pkl".format(
                                             cfg['model']['arch'],
                                             cfg['data']['dataset']))
                torch.save(state, save_path)

        if (i_train_iter + 1) == cfg['training']['train_iters']:
            flag = False
            break
        i_train_iter += 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="config")
    parser.add_argument(
        "--config",
        nargs="?",
        type=str,
        default="configs/miccai2008-anatomicalstructure.yml",
        help="Configuration file to use"
    )

    args = parser.parse_args()

    with open(args.config) as fp:
        cfg = yaml.load(fp)

    run_id = random.randint(1,100000)
    logdir = os.path.join('runs', os.path.basename(args.config)[:-4] , str(run_id))
    writer = SummaryWriter(log_dir=logdir)

    # Display Tensorboard
    print('TensorBoard::RUNDIR: {}'.format(logdir))

    # Display Config
    print('\x1b[1;32;44m#######\nCONFIG:')
    for key, value_dict in cfg.items():
        print('{}:'.format(key))
        for k, v in value_dict.items():
            print('\t\t{}: {}'.format(k, v))
    print('#######\n\x1b[0m')

    shutil.copy(args.config, logdir)

    logger = get_logger(logdir)
    logger.info('Let the games begin')
    subprocess.Popen("kill $(lsof -t -c tensorboa -a -i:6006)", shell=True)
    subprocess.Popen(['tensorboard', '--logdir', '{}'.format(logdir)])

    train(cfg, writer, logger)

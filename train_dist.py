import os
import sys
import time
import numpy as np
from tqdm import tqdm
import json
import argparse
import pickle
import torch
import torch.optim as optim
from torch.utils.data import DataLoader
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel
from matplotlib import pyplot as plt
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR
sys.path.append(os.path.join(ROOT_DIR, 'pointnet2'))

from utils import get_scheduler, setup_logger
from models import GroupFreeDetector, get_loss
from models import APCalculator, parse_predictions, parse_groundtruths


def parse_option():
    parser = argparse.ArgumentParser()
    # Model
    parser.add_argument('--width', default=1, type=int, help='backbone width')
    parser.add_argument('--num_target', type=int, default=256, help='Proposal number [default: 256]')
    parser.add_argument('--sampling', default='kps', type=str, help='Query points sampling method (kps, fps)')
    # Transformer
    parser.add_argument('--nhead', default=8, type=int, help='multi-head number')
    parser.add_argument('--num_decoder_layers', default=8, type=int, help='number of decoder layers')
    parser.add_argument('--dim_feedforward', default=4096, type=int, help='dim_feedforward')
    parser.add_argument('--transformer_dropout', default=0.1, type=float, help='transformer_dropout')
    parser.add_argument('--transformer_activation', default='relu', type=str, help='transformer_activation')
    parser.add_argument('--self_position_embedding', default='loc_learned', type=str,
                        help='position_embedding in self attention (none, xyz_learned, loc_learned)')
    parser.add_argument('--cross_position_embedding', default='xyz_learned', type=str,
                        help='position embedding in cross attention (none, xyz_learned)')

    # Loss
    parser.add_argument('--query_points_generator_loss_coef', default=0.1, type=float)
    parser.add_argument('--obj_loss_coef', default=1.0, type=float, help='Loss weight for objectness loss')
    parser.add_argument('--box_loss_coef', default=1.0, type=float, help='Loss weight for box loss')
    parser.add_argument('--sem_cls_loss_coef', default=0.0, type=float, help='Loss weight for classification loss')
    parser.add_argument('--center_loss_type', default='smoothl1', type=str, help='(smoothl1, l1)')
    parser.add_argument('--center_delta', default=1.0, type=float, help='delta for smoothl1 loss in center loss')
    parser.add_argument('--size_loss_type', default='smoothl1', type=str, help='(smoothl1, l1)')
    parser.add_argument('--size_delta', default=1.0, type=float, help='delta for smoothl1 loss in size loss')
    parser.add_argument('--heading_loss_type', default='smoothl1', type=str, help='(smoothl1, l1)')
    parser.add_argument('--heading_delta', default=1.0, type=float, help='delta for smoothl1 loss in heading loss')
    parser.add_argument('--query_points_obj_topk', default=4, type=int, help='query_points_obj_topk')
    parser.add_argument('--size_cls_agnostic', action='store_true', help='Use class-agnostic size prediction.')

    # Data
    parser.add_argument('--batch_size', type=int, default=2, help='Batch Size per GPU during training [default: 8]')
    parser.add_argument('--val_batch_size', type=int, default=4, help='Batch Size per GPU during training [default: 8]')
    parser.add_argument('--dataset', default='scannet', help='Dataset name. sunrgbd or scannet. [default: scannet]')
    parser.add_argument('--num_point', type=int, default=450000, help='Point Number [default: 50000]')
    parser.add_argument('--data_root', default='data', help='data root path')
    parser.add_argument('--use_height', action='store_true', help='Use height signal in input.')
    parser.add_argument('--use_color', action='store_true', help='Use RGB color in input.')
    parser.add_argument('--augment', action='store_true', help='Use Data Augmentation in SUN RGB-D dataset.')
    parser.add_argument('--use_sunrgbd_v2', action='store_true', help='Use V2 box labels for SUN RGB-D dataset')
    parser.add_argument('--load_all_data', action='store_true', help='Loads all the data into memory if True, '
                                                                     'otherwise only loads a single batch data.')
    parser.add_argument('--num_workers', type=int, default=4, help='num of workers to use')

    # Training
    parser.add_argument('--start_epoch', type=int, default=1, help='Epoch to run [default: 1]')
    parser.add_argument('--max_epoch', type=int, default=700, help='Epoch to run [default: 180]')
    parser.add_argument('--optimizer', type=str, default='adamW', help='optimizer')
    parser.add_argument('--momentum', type=float, default=0.9, help='momentum for SGD')
    parser.add_argument('--weight_decay', type=float, default=0.0005,
                        help='Optimization L2 weight decay [default: 0.0005]')
    parser.add_argument('--learning_rate', type=float, default=0.004,
                        help='Initial learning rate for all except decoder [default: 0.004]')
    parser.add_argument('--decoder_learning_rate', type=float, default=0.0004,
                        help='Initial learning rate for decoder [default: 0.0004]')
    parser.add_argument('--lr-scheduler', type=str, default='step',
                        choices=["step", "cosine"], help="learning rate scheduler")
    parser.add_argument('--warmup-epoch', type=int, default=-1, help='warmup epoch')
    parser.add_argument('--warmup-multiplier', type=int, default=100, help='warmup multiplier')
    parser.add_argument('--lr_decay_epochs', type=int, default=[50, 80], nargs='+',
                        help='for step scheduler. where to decay lr, can be a list')
    parser.add_argument('--lr_decay_rate', type=float, default=0.3,
                        help='for step scheduler. decay rate for learning rate')
    parser.add_argument('--clip_norm', default=0.1, type=float,
                        help='gradient clipping max norm')
    parser.add_argument('--bn_momentum', type=float, default=0.1, help='Default bn momeuntum')
    parser.add_argument('--syncbn', action='store_true', help='whether to use sync bn')

    # io
    parser.add_argument('--checkpoint_path', default=None, help='Model checkpoint path [default: None]')
    parser.add_argument('--log_dir', default='log', help='Dump dir to save model checkpoint [default: log]')
    parser.add_argument('--print_freq', type=int, default=1, help='print frequency')
    parser.add_argument('--save_freq', type=int, default=10, help='save frequency')
    parser.add_argument('--val_freq', type=int, default=10, help='val frequency')
    parser.add_argument('--train_eval_freq', type=int, default=40, help='train eval frequency')

    # others
    parser.add_argument("--local_rank", type=int, help='local rank for DistributedDataParallel', default=2)
    parser.add_argument('--ap_iou_thresholds', type=float, default=[0.25, 0.5], nargs='+',
                        help='A list of AP IoU thresholds [default: 0.25,0.5]')
    parser.add_argument("--rng_seed", type=int, default=0, help='manual seed')
    args, unparsed = parser.parse_known_args()

    return args


class EarlyStopping:
    """Early stops the training if validation loss doesn't improve after a given patience."""

    def __init__(self, patience=7, verbose=False, delta=0, path='checkpoint.pt', trace_func=print):
        """
        Args:
            patience (int): How long to wait after last time validation loss improved.
                            Default: 7
            verbose (bool): If True, prints a message for each validation loss improvement.
                            Default: False
            delta (float): Minimum change in the monitored quantity to qualify as an improvement.
                            Default: 0
            path (str): Path for the checkpoint to be saved to.
                            Default: 'checkpoint.pt'
            trace_func (function): trace print function.
                            Default: print
        """
        self.patience = patience
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_loss_min = np.Inf
        self.delta = delta
        self.path = path
        self.trace_func = trace_func

    def __call__(self, val_loss, model, args, epoch, optimizer, scheduler):

        score = -val_loss

        if self.best_score is None:
            self.best_score = score
            self.save_checkpoint(val_loss, model, epoch, optimizer, scheduler)
        elif score < self.best_score + self.delta:
            self.counter += 1
            self.trace_func(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = score
            self.save_checkpoint(val_loss, model, epoch, optimizer, scheduler)
            self.counter = 0

    def save_checkpoint(self, val_loss, model, epoch, optimizer, scheduler):
        """Saves model when validation loss decrease."""
        logger.info(
                f'Validation loss decreased ({self.val_loss_min:.6f} --> {val_loss:.6f}).')
        if epoch % opt.save_freq:
            logger.info('==> Saving model...')
            save_checkpoint(opt, epoch, model, optimizer, scheduler, save_cur=True)
        self.val_loss_min = val_loss


def load_checkpoint(args, model, optimizer, scheduler):
    logger.info("=> loading checkpoint '{}'".format(args.checkpoint_path))

    checkpoint = torch.load(args.checkpoint_path, map_location='cpu')
    args.start_epoch = checkpoint['epoch'] + 1
    model.load_state_dict(checkpoint['model'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    scheduler.load_state_dict(checkpoint['scheduler'])

    logger.info("=> loaded successfully '{}' (epoch {})".format(args.checkpoint_path, checkpoint['epoch']))
    # # args.start_epoch = int(checkpoint['epoch']) + 1
    # model.load_state_dict(checkpoint['model'])
    # optimizer.load_state_dict(checkpoint['optimizer'])
    # logger.info("=> loaded successfully '{}' (epoch {})".format(args.checkpoint_path, checkpoint['epoch']))
    # # scheduler.load_state_dict(checkpoint['scheduler'])
    # scheduler._last_lr = checkpoint['scheduler']['_last_lr']
    # logger.info(f'=> Modified scheduler')
    # logger.info(f'=> Printing scheduler: {scheduler.state_dict()}')

    del checkpoint
    torch.cuda.empty_cache()


def save_checkpoint(args, epoch, model, optimizer, scheduler, save_cur=False):
    logger.info('==> Saving...')
    state = {
        'config': args,
        'save_path': '',
        'model': model.state_dict(),
        'optimizer': optimizer.state_dict(),
        'scheduler': scheduler.state_dict(),
        'epoch': epoch,
    }

    if save_cur or not (epoch % args.save_freq):
        state['save_path'] = os.path.join(args.log_dir, f'ckpt_epoch_{epoch}.pth')
        torch.save(state, os.path.join(args.log_dir, f'ckpt_epoch_{epoch}.pth'))
        logger.info("Saved in {}".format(os.path.join(args.log_dir, f'ckpt_epoch_{epoch}.pth')))
    else:
        # state['save_path'] = 'current.pth'
        # torch.save(state, os.path.join(args.log_dir, 'current.pth'))
        print("not saving checkpoint")
        pass


def get_loader(args):
    # Init datasets and dataloaders
    def my_worker_init_fn(worker_id):
        np.random.seed(np.random.get_state()[1][0] + worker_id)

    # Create Dataset and Dataloader
    if args.dataset == 'sunrgbd':
        from sunrgbd.sunrgbd_detection_dataset import SunrgbdDetectionVotesDataset
        from sunrgbd.model_util_sunrgbd import SunrgbdDatasetConfig

        DATASET_CONFIG = SunrgbdDatasetConfig()
        TRAIN_DATASET = SunrgbdDetectionVotesDataset('train', num_points=args.num_point,
                                                     augment=True if args.augment else False,
                                                     use_color=True if args.use_color else False,
                                                     use_height=True if args.use_height else False,
                                                     use_v1=(not args.use_sunrgbd_v2),
                                                     data_root=args.data_root,
                                                     load_all_data=args.load_all_data)
        TEST_DATASET = SunrgbdDetectionVotesDataset('val', num_points=args.num_point,
                                                    augment=False,
                                                    use_color=True if args.use_color else False,
                                                    use_height=True if args.use_height else False,
                                                    use_v1=(not args.use_sunrgbd_v2),
                                                    data_root=args.data_root,
                                                    load_all_data=args.load_all_data)
    elif args.dataset == 'scannet':
        sys.path.append(os.path.join(ROOT_DIR, 'scannet'))
        from scannet.scannet_detection_dataset import ScannetDetectionDataset
        from scannet.model_util_scannet import ScannetDatasetConfig

        DATASET_CONFIG = ScannetDatasetConfig()
        TRAIN_DATASET = ScannetDetectionDataset('train', num_points=args.num_point,
                                                augment=True,
                                                use_color=True if args.use_color else False,
                                                use_height=True if args.use_height else False,
                                                data_root=args.data_root)
        TEST_DATASET = ScannetDetectionDataset('val', num_points=args.num_point,
                                               augment=False,
                                               use_color=True if args.use_color else False,
                                               use_height=True if args.use_height else False,
                                               data_root=args.data_root)
    else:
        raise NotImplementedError(f'Unknown dataset {args.dataset}. Exiting...')

    print(f"train_len: {len(TRAIN_DATASET)}, test_len: {len(TEST_DATASET)}")

    train_sampler = torch.utils.data.distributed.DistributedSampler(TRAIN_DATASET)
    train_loader = torch.utils.data.DataLoader(TRAIN_DATASET,
                                               batch_size=args.batch_size,
                                               shuffle=False,
                                               num_workers=args.num_workers,
                                               worker_init_fn=my_worker_init_fn,
                                               pin_memory=True,
                                               sampler=train_sampler,
                                               drop_last=True)
    train_eval_loader = torch.utils.data.DataLoader(TRAIN_DATASET,
                                                    batch_size=args.val_batch_size,
                                                    shuffle=False,
                                                    num_workers=args.num_workers,
                                                    worker_init_fn=my_worker_init_fn,
                                                    pin_memory=True,
                                                    drop_last=True)

    # test_sampler = torch.utils.data.distributed.DistributedSampler(TEST_DATASET, shuffle=False)
    test_loader = torch.utils.data.DataLoader(TEST_DATASET,
                                              batch_size=args.val_batch_size,
                                              shuffle=False,
                                              num_workers=args.num_workers,
                                              worker_init_fn=my_worker_init_fn,
                                              pin_memory=True,
                                              drop_last=False)
    print(f"train_loader_len: {len(train_loader)}, test_loader_len: {len(test_loader)}")

    return train_loader, train_eval_loader, test_loader, DATASET_CONFIG


def get_model(args, DATASET_CONFIG):
    if args.use_height:
        num_input_channel = int(args.use_color) * 3 + 1
    else:
        num_input_channel = int(args.use_color) * 3
    model = GroupFreeDetector(num_class=DATASET_CONFIG.num_class,
                              num_heading_bin=DATASET_CONFIG.num_heading_bin,
                              num_size_cluster=DATASET_CONFIG.num_size_cluster,
                              mean_size_arr=DATASET_CONFIG.mean_size_arr,
                              input_feature_dim=num_input_channel,
                              width=args.width,
                              bn_momentum=args.bn_momentum,
                              sync_bn=True if args.syncbn else False,
                              num_proposal=args.num_target,
                              sampling=args.sampling,
                              dropout=args.transformer_dropout,
                              activation=args.transformer_activation,
                              nhead=args.nhead,
                              num_decoder_layers=args.num_decoder_layers,
                              dim_feedforward=args.dim_feedforward,
                              self_position_embedding=args.self_position_embedding,
                              cross_position_embedding=args.cross_position_embedding,
                              size_cls_agnostic=True if args.size_cls_agnostic else False)

    criterion = get_loss
    return model, criterion


def main(args):
    train_loader, train_eval_loader, test_loader, DATASET_CONFIG = get_loader(args)
    n_data = len(train_loader.dataset)
    logger.info(f"length of training dataset: {n_data}")
    n_data = len(test_loader.dataset)
    logger.info(f"length of testing dataset: {n_data}")
    print(f'use_color: {args.use_color}')
    print(f'augment: {args.augment}')
    print(f'start_epoch: {args.start_epoch}')
    model, criterion = get_model(args, DATASET_CONFIG)
    if dist.get_rank() == 0:
        logger.info(str(model))
    # optimizer
    if args.optimizer == 'adamW':
        param_dicts = [
            {"params": [p for n, p in model.named_parameters() if "decoder" not in n and p.requires_grad]},
            {
                "params": [p for n, p in model.named_parameters() if "decoder" in n and p.requires_grad],
                "lr": args.decoder_learning_rate,
            },
        ]
        optimizer = optim.AdamW(param_dicts,
                                lr=args.learning_rate,
                                weight_decay=args.weight_decay)
    else:
        raise NotImplementedError

    scheduler = get_scheduler(optimizer, len(train_loader), args)
    model = model.cuda()
    model = DistributedDataParallel(model, device_ids=[args.local_rank], broadcast_buffers=False)

    if args.checkpoint_path:
        assert os.path.isfile(args.checkpoint_path)
        load_checkpoint(args, model, optimizer, scheduler)

    # Used for AP calculation
    CONFIG_DICT = {'remove_empty_box': True, 'use_3d_nms': True,
                   'nms_iou': 0.1, 'use_old_type_nms': False, 'cls_nms': True,
                   'per_class_proposal': True, 'conf_thresh': 0.1,
                   'dataset_config': DATASET_CONFIG}

    val_metrics_dict = defaultdict(list)
    train_metrics_dict = defaultdict(list)
    train_loss_dict = defaultdict(list)
    early_stopping = EarlyStopping(patience=20, verbose=True)
    for epoch in range(args.start_epoch, args.max_epoch + 1):
        train_loader.sampler.set_epoch(epoch)

        tic = time.time()

        train_loss_dict = train_one_epoch(epoch, train_loader, DATASET_CONFIG, model, criterion, optimizer, scheduler,
                                          args, train_loss_dict)

        logger.info('epoch {}, total time {:.2f}, '
                    'lr_base {:.5f}, lr_decoder {:.5f}'.format(epoch, (time.time() - tic),
                                                               optimizer.param_groups[0]['lr'],
                                                               optimizer.param_groups[1]['lr']))

        with open(os.path.join(args.log_dir, 'train_loss_dict.pkl'), 'wb') as f:
            pickle.dump(train_loss_dict, f)
            print(f"{os.path.join(args.log_dir, 'train_loss_dict.pkl')} saved successfully !!")

        if dist.get_rank() == 0:
            if (epoch % args.train_eval_freq == 0) or (epoch == args.val_freq):
                train_metrics_dict, _ = evaluate_one_epoch(train_eval_loader, DATASET_CONFIG, CONFIG_DICT,
                                                           args.ap_iou_thresholds, model, criterion, args,
                                                           train_metrics_dict)
                with open(os.path.join(args.log_dir, 'train_metrics_dict.pkl'), 'wb') as f:
                    pickle.dump(train_metrics_dict, f)
                    print(f"{os.path.join(args.log_dir, 'train_metrics_dict.pkl')} saved successfully !!")

            if epoch % args.val_freq == 0:
                val_metrics_dict, valid_loss = evaluate_one_epoch(test_loader, DATASET_CONFIG, CONFIG_DICT,
                                                                  args.ap_iou_thresholds,
                                                                  model, criterion, args, val_metrics_dict)
                with open(os.path.join(args.log_dir, 'val_metrics_dict.pkl'), 'wb') as f:
                    pickle.dump(val_metrics_dict, f)
                    print(f"{os.path.join(args.log_dir, 'val_metrics_dict.pkl')} saved successfully !!")

                early_stopping(valid_loss, model, args, epoch, optimizer, scheduler)
                logger.info(f"Validation Loss: {valid_loss}")
                if early_stopping.early_stop:
                    logger.info("Early stopping")
                    break
            # save model
            save_checkpoint(args, epoch, model, optimizer, scheduler)

    evaluate_one_epoch(test_loader, DATASET_CONFIG, CONFIG_DICT, args.ap_iou_thresholds, model, criterion, args,
                       defaultdict(list))

    save_checkpoint(args, 'last', model, optimizer, scheduler, save_cur=True)
    logger.info("Saved in {}".format(os.path.join(args.log_dir, f'ckpt_epoch_last.pth')))
    return os.path.join(args.log_dir, f'ckpt_epoch_last.pth')


def train_one_epoch(epoch, train_loader, DATASET_CONFIG, model, criterion, optimizer, scheduler, config,
                    train_stat_dict):
    stat_dict = {}  # collect statistics
    model.train()  # set model to training mode
    for batch_idx, batch_data_label in tqdm(enumerate(train_loader), f"Epoch-{epoch}, Batches-{len(train_loader)}: "
                                                                     f"Running training step ..."):
        for key in batch_data_label:
            batch_data_label[key] = batch_data_label[key].cuda(non_blocking=True)
        inputs = {'point_clouds': batch_data_label['point_clouds']}

        # Forward pass
        end_points = model(inputs)

        # Compute loss and gradients, update parameters.
        for key in batch_data_label:
            assert (key not in end_points)
            end_points[key] = batch_data_label[key]
        loss, end_points = criterion(end_points, DATASET_CONFIG,
                                     num_decoder_layers=config.num_decoder_layers,
                                     query_points_generator_loss_coef=config.query_points_generator_loss_coef,
                                     obj_loss_coef=config.obj_loss_coef,
                                     box_loss_coef=config.box_loss_coef,
                                     sem_cls_loss_coef=config.sem_cls_loss_coef,
                                     query_points_obj_topk=config.query_points_obj_topk,
                                     center_loss_type=config.center_loss_type,
                                     center_delta=config.center_delta,
                                     size_loss_type=config.size_loss_type,
                                     size_delta=config.size_delta,
                                     heading_loss_type=config.heading_loss_type,
                                     heading_delta=config.heading_delta,
                                     size_cls_agnostic=config.size_cls_agnostic)

        optimizer.zero_grad()
        loss.backward()
        if config.clip_norm > 0:
            grad_total_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), config.clip_norm)
        optimizer.step()
        scheduler.step()

        # Accumulate statistics and print out
        stat_dict['grad_norm'] = grad_total_norm
        for key in end_points:
            if 'loss' in key or 'acc' in key or 'ratio' in key:
                if key not in stat_dict: stat_dict[key] = 0
                if isinstance(end_points[key], float):
                    stat_dict[key] += end_points[key]
                else:
                    stat_dict[key] += end_points[key].item()

    if epoch % config.val_freq == 0:
        for key in stat_dict.keys():
            stat_dict[key] = stat_dict[key] / len(train_loader)

    if epoch % config.val_freq == 0:
        logger.info(f'Train: [{epoch}]' + ''.join(
            [f'{key} {stat_dict[key]:.4f} \t'
             for key in sorted(stat_dict.keys()) if 'loss' not in key]))
        # logger.info(f"grad_norm: {stat_dict['grad_norm']}")
        logger.info(''.join([f'{key} {stat_dict[key]:.4f} \t'
                             for key in sorted(stat_dict.keys()) if
                             'loss' in key and 'proposal_' not in key and 'last_' not in key and 'head_' not in key]))
        logger.info(''.join([f'{key} {stat_dict[key]:.4f} \t'
                             for key in sorted(stat_dict.keys()) if 'last_' in key]))
        logger.info(''.join([f'{key} {stat_dict[key]:.4f} \t'
                             for key in sorted(stat_dict.keys()) if 'proposal_' in key]))
        for ihead in range(config.num_decoder_layers - 2, -1, -1):
            logger.info(''.join([f'{key} {stat_dict[key]:.4f} \t'
                                 for key in sorted(stat_dict.keys()) if f'{ihead}head_' in key]))

        if epoch % config.val_freq == 0:
            for key in stat_dict.keys():
                if ('loss' in key) or ('acc' in key):
                    train_stat_dict[key].append(stat_dict[key])
    return train_stat_dict


def evaluate_one_epoch(test_loader, DATASET_CONFIG, CONFIG_DICT, AP_IOU_THRESHOLDS, model, criterion, config,
                       plots_dict):
    stat_dict = {}
    valid_losses = []

    if config.num_decoder_layers > 0:
        prefixes = ['last_', 'proposal_'] + [f'{i}head_' for i in range(config.num_decoder_layers - 1)]
    else:
        prefixes = ['proposal_']  # only proposal
    ap_calculator_list = [APCalculator(iou_thresh, DATASET_CONFIG.class2type) \
                          for iou_thresh in AP_IOU_THRESHOLDS]
    mAPs = [[iou_thresh, {k: 0 for k in prefixes}] for iou_thresh in AP_IOU_THRESHOLDS]

    model.eval()  # set model to eval mode (for bn and dp)
    batch_pred_map_cls_dict = {k: [] for k in prefixes}
    batch_gt_map_cls_dict = {k: [] for k in prefixes}

    for batch_idx, batch_data_label in tqdm(enumerate(test_loader), f"Batches - {len(test_loader)}: "
                                                                    f"Running validation steps ..."):
        for key in batch_data_label:
            batch_data_label[key] = batch_data_label[key].cuda(non_blocking=True)

        # Forward pass
        inputs = {'point_clouds': batch_data_label['point_clouds']}
        with torch.no_grad():
            end_points = model(inputs)

        # Compute loss
        for key in batch_data_label:
            assert (key not in end_points)
            end_points[key] = batch_data_label[key]
        loss, end_points = criterion(end_points, DATASET_CONFIG,
                                     num_decoder_layers=config.num_decoder_layers,
                                     query_points_generator_loss_coef=config.query_points_generator_loss_coef,
                                     obj_loss_coef=config.obj_loss_coef,
                                     box_loss_coef=config.box_loss_coef,
                                     sem_cls_loss_coef=config.sem_cls_loss_coef,
                                     query_points_obj_topk=config.query_points_obj_topk,
                                     center_loss_type=config.center_loss_type,
                                     center_delta=config.center_delta,
                                     size_loss_type=config.size_loss_type,
                                     size_delta=config.size_delta,
                                     heading_loss_type=config.heading_loss_type,
                                     heading_delta=config.heading_delta,
                                     size_cls_agnostic=config.size_cls_agnostic)

        valid_losses.append(loss.item())

        # Accumulate statistics and print out
        for key in end_points:
            if 'loss' in key or 'acc' in key or 'ratio' in key:
                if key not in stat_dict: stat_dict[key] = 0
                if isinstance(end_points[key], float):
                    stat_dict[key] += end_points[key]
                else:
                    stat_dict[key] += end_points[key].item()

        for prefix in prefixes:
            batch_pred_map_cls = parse_predictions(end_points, CONFIG_DICT, prefix,
                                                   size_cls_agnostic=config.size_cls_agnostic)
            batch_gt_map_cls = parse_groundtruths(end_points, CONFIG_DICT,
                                                  size_cls_agnostic=config.size_cls_agnostic)
            batch_pred_map_cls_dict[prefix].append(batch_pred_map_cls)
            batch_gt_map_cls_dict[prefix].append(batch_gt_map_cls)

        if (batch_idx + 1) % config.print_freq == 0:
            logger.info(f'Eval: [{batch_idx + 1}/{len(test_loader)}]  ' + ''.join(
                [f'{key} {stat_dict[key] / (float(batch_idx + 1)):.4f} \t'
                 for key in sorted(stat_dict.keys()) if 'loss' not in key]))
            logger.info(''.join([f'{key} {stat_dict[key] / (float(batch_idx + 1)):.4f} \t'
                                 for key in sorted(stat_dict.keys()) if
                                 'loss' in key and 'proposal_' not in key and 'last_' not in key and 'head_' not in key]))
            logger.info(''.join([f'{key} {stat_dict[key] / (float(batch_idx + 1)):.4f} \t'
                                 for key in sorted(stat_dict.keys()) if 'last_' in key]))
            logger.info(''.join([f'{key} {stat_dict[key] / (float(batch_idx + 1)):.4f} \t'
                                 for key in sorted(stat_dict.keys()) if 'proposal_' in key]))
            for ihead in range(config.num_decoder_layers - 2, -1, -1):
                logger.info(''.join([f'{key} {stat_dict[key] / (float(batch_idx + 1)):.4f} \t'
                                     for key in sorted(stat_dict.keys()) if f'{ihead}head_' in key]))
    mAP = 0.0
    for prefix in prefixes:
        for (batch_pred_map_cls, batch_gt_map_cls) in zip(batch_pred_map_cls_dict[prefix],
                                                          batch_gt_map_cls_dict[prefix]):
            for ap_calculator in ap_calculator_list:
                ap_calculator.step(batch_pred_map_cls, batch_gt_map_cls)
        # Evaluate average precision
        for i, ap_calculator in enumerate(ap_calculator_list):
            metrics_dict = ap_calculator.compute_metrics()
            logger.info(f'=====================>{prefix} IOU THRESH: {AP_IOU_THRESHOLDS[i]}<=====================')
            for key in metrics_dict:
                logger.info(f'{key} {metrics_dict[key]}')
            if prefix == 'last_' and ap_calculator.ap_iou_thresh > 0.3:
                mAP = metrics_dict['mAP']
            mAPs[i][1][prefix] = metrics_dict['mAP']
            plots_dict[f'{prefix}_{ap_calculator.ap_iou_thresh}_AP'].append(metrics_dict['mAP'])
            plots_dict[f'{prefix}_{ap_calculator.ap_iou_thresh}_AR'].append(metrics_dict['AR'])
            ap_calculator.reset()

    for mAP in mAPs:
        logger.info(f'IoU[{mAP[0]}]:\t' + ''.join([f'{key}: {mAP[1][key]:.4f} \t' for key in sorted(mAP[1].keys())]))

    valid_loss = np.average(valid_losses)

    return plots_dict, valid_loss


def plot_metrics(plots_dict_1, val_freq, save_dir, plots_dict_2=None, label_1=None, label_2=None):
    if not os.path.exists(save_dir):
        os.mkdir(save_dir)
    for key, metrics_list in plots_dict_1.items():
        plt.figure()
        plt.title(key)
        plt.plot([(i + 1) * val_freq for i in range(len(metrics_list))], metrics_list, color='r', label=label_1)
        if (plots_dict_2 is not None) and (key in plots_dict_2):
            metrics_list_2 = plots_dict_2[key]
            plt.plot([(i + 1) * val_freq for i in range(len(metrics_list_2))], metrics_list_2, color='g', label=label_2)
            plt.legend()
        plt.xlabel("Epochs")
        plt.ylabel("Metrics")
        plt.savefig(save_dir + f'/{key}.jpg')
        plt.close()


if __name__ == '__main__':
    opt = parse_option()
    torch.cuda.set_device(opt.local_rank)
    torch.distributed.init_process_group(backend='nccl', init_method='env://')
    torch.backends.cudnn.enabled = True
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = True

    LOG_DIR = os.path.join(opt.log_dir, 'group_free',
                           f'{opt.dataset}_{int(time.time())}', f'{np.random.randint(100000000)}')
    while os.path.exists(LOG_DIR):
        LOG_DIR = os.path.join(opt.log_dir, 'group_free',
                               f'{opt.dataset}_{int(time.time())}', f'{np.random.randint(100000000)}')
    opt.log_dir = LOG_DIR
    os.makedirs(opt.log_dir, exist_ok=True)

    logger = setup_logger(output=opt.log_dir, distributed_rank=dist.get_rank(), name="group-free")
    if dist.get_rank() == 0:
        path = os.path.join(opt.log_dir, "config.json")
        with open(path, 'w') as f:
            json.dump(vars(opt), f, indent=2)
        logger.info("Full config saved to {}".format(path))
        logger.info(str(vars(opt)))

    ckpt_path = main(opt)
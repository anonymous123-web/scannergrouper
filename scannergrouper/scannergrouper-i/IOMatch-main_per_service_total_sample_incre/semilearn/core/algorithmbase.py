import os
import contextlib
import numpy as np
from inspect import signature
from collections import OrderedDict
from sklearn.metrics import accuracy_score, balanced_accuracy_score, precision_score, recall_score, f1_score, \
    confusion_matrix

import torch
import torch.nn.functional as F
from torch.cuda.amp import autocast, GradScaler

from semilearn.core.hooks import Hook, get_priority, CheckpointHook, TimerHook, LoggingHook, DistSamplerSeedHook, \
    ParamUpdateHook, EvaluationHook, EMAHook
from semilearn.core.utils import get_dataset, get_data_loader, get_optimizer, get_cosine_schedule_with_warmup, \
    Bn_Controller


class AlgorithmBase:
    """
        Base class for algorithms
        init algorithm specific parameters and common parameters
        
        Args:
            - args (`argparse`):
                algorithm arguments
            - net_builder (`callable`):
                network loading function
            - tb_log (`TBLog`):
                tensorboard logger
            - logger (`logging.Logger`):
                logger to use
    """

    def __init__(
            self,
            args,
            net_builder,
            tb_log=None,
            logger=None,
            **kwargs):

        # common arguments
        self.tb_dict = None
        self.args = args
        self.num_classes = args.num_classes
        self.ema_m = args.ema_m
        self.epochs = args.epoch
        self.num_train_iter = args.num_train_iter
        self.num_eval_iter = args.num_eval_iter
        self.num_log_iter = args.num_log_iter
        self.num_iter_per_epoch = int(self.num_train_iter // self.epochs)
        self.lambda_u = args.ulb_loss_ratio
        self.use_cat = args.use_cat
        self.use_amp = args.use_amp
        self.clip_grad = args.clip_grad
        self.save_name = args.save_name
        self.save_dir = args.save_dir
        self.resume = args.resume
        self.algorithm = args.algorithm

        # commaon utils arguments
        self.tb_log = tb_log
        self.print_fn = print if logger is None else logger.info
        self.ngpus_per_node = torch.cuda.device_count()
        self.loss_scaler = GradScaler()
        self.amp_cm = autocast if self.use_amp else contextlib.nullcontext
        self.gpu = args.gpu
        self.rank = args.rank
        self.distributed = args.distributed
        self.world_size = args.world_size

        # common model related parameters
        self.epoch = 0
        self.it = 0
        self.best_eval_acc, self.best_it = 0.0, 0
        self.bn_controller = Bn_Controller()
        self.net_builder = net_builder
        self.ema = None

        # build dataset
        self.dataset_dict = self.set_dataset()

        # build data loader
        self.loader_dict = self.set_data_loader()

        # cv, nlp, speech builder different arguments
        self.model = self.set_model()
        self.ema_model = self.set_ema_model()

        # build optimizer and scheduler
        self.optimizer, self.scheduler = self.set_optimizer()

        # other arguments specific to the algorithm
        # self.init(**kwargs)

        # set common hooks during training
        self._hooks = []  # record underlying hooks 
        self.hooks_dict = OrderedDict()  # actual object to be used to call hooks
        self.set_hooks()

    def init(self, **kwargs):
        """
        algorithm specific init function, to add parameters into class
        """
        raise NotImplementedError

    def set_dataset(self):
        if self.rank != 0 and self.distributed:
            torch.distributed.barrier()
        dataset_dict = get_dataset(self.args, self.algorithm, self.args.dataset, self.args.date,self.args.num_labels,
                                   self.args.num_classes, self.args.data_dir)
        self.args.ulb_dest_len = len(dataset_dict['train_ulb']) if dataset_dict['train_ulb'] is not None else 0
        self.args.lb_dest_len = len(dataset_dict['train_lb'])
        self.print_fn(
            "unlabeled data number: {}, labeled data number {}".format(self.args.ulb_dest_len, self.args.lb_dest_len))
        if self.rank == 0 and self.distributed:
            torch.distributed.barrier()
        return dataset_dict

    def set_data_loader(self):
        self.print_fn("Create train and test data loaders")
        loader_dict = {}
        print('setting')
        loader_dict['train_lb'] = get_data_loader(self.args,
                                                  self.dataset_dict['train_lb'],
                                                  self.args.batch_size,
                                                  data_sampler=self.args.train_sampler,
                                                  num_iters=self.num_train_iter,
                                                  num_epochs=self.epochs,
                                                  num_workers=self.args.num_workers,
                                                  distributed=self.distributed)

        loader_dict['train_ulb'] = get_data_loader(self.args,
                                                   self.dataset_dict['train_ulb'],
                                                   self.args.batch_size * self.args.uratio,
                                                   data_sampler=self.args.train_sampler,
                                                   num_iters=self.num_train_iter,
                                                   num_epochs=self.epochs,
                                                   num_workers=2 * self.args.num_workers,
                                                   distributed=self.distributed)

        loader_dict['eval'] = get_data_loader(self.args,
                                              self.dataset_dict['eval'],
                                              self.args.eval_batch_size,
                                              # make sure data_sampler is None for evaluation
                                              data_sampler=None,
                                              num_workers=self.args.num_workers,
                                              drop_last=False)

        if self.dataset_dict['test'] is not None:
            if isinstance(self.dataset_dict['test'], dict):
                loader_dict['test'] = {}
                for k, v in self.dataset_dict['test'].items():
                    if v is not None:
                        loader_dict['test'][k] = get_data_loader(self.args,
                                                                 self.dataset_dict['test'][k],
                                                                 self.args.eval_batch_size,
                                                                 # make sure data_sampler is None for evaluation
                                                                 data_sampler=None,
                                                                 num_workers=self.args.num_workers,
                                                                 drop_last=False)
            else:
                loader_dict['test'] = get_data_loader(self.args,
                                                      self.dataset_dict['test'],
                                                      self.args.eval_batch_size,
                                                      # make sure data_sampler is None for evaluation
                                                      data_sampler=None,
                                                      num_workers=self.args.num_workers,
                                                      drop_last=False)
        self.print_fn(f'[!] data loader keys: {loader_dict.keys()}')
        return loader_dict

    def set_optimizer(self):
        self.print_fn("Create optimizer and scheduler")
        optimizer = get_optimizer(self.model, self.args.optim, self.args.lr, self.args.momentum, self.args.weight_decay,
                                  self.args.layer_decay)
        scheduler = get_cosine_schedule_with_warmup(optimizer,
                                                    self.num_train_iter,
                                                    num_warmup_steps=self.args.num_warmup_iter)
        return optimizer, scheduler

    def set_model(self):
        model = self.net_builder(num_classes=self.num_classes, pretrained=self.args.use_pretrain,
                                 pretrained_path=self.args.pretrain_path)
        return model

    def set_ema_model(self):
        """
        initialize ema model from model
        """
        ema_model = self.net_builder(num_classes=self.num_classes)
        ema_model.load_state_dict(self.model.state_dict())
        return ema_model

    def set_hooks(self):
        """
        register necessary training hooks
        """
        # parameter update hook is called inside each train_step
        self.register_hook(TimerHook(), None, "HIGHEST")
        self.register_hook(EMAHook(), None, "HIGHEST")
        self.register_hook(EvaluationHook(), None, "HIGHEST")
        self.register_hook(CheckpointHook(), None, "VERY_HIGH")
        self.register_hook(DistSamplerSeedHook(), None, "HIGH")
        self.register_hook(LoggingHook(), None, "LOW")

        # for hooks to be called in train_step, name it for simpler calling
        self.register_hook(ParamUpdateHook(), "ParamUpdateHook")

    def process_batch(self, **kwargs):
        """
        process batch data, send data to cuda
        NOTE **kwargs should have the same arguments to train_step function as keys to work properly
        """
        input_args = signature(self.train_step).parameters
        input_args = list(input_args.keys())
        input_dict = {}

        for arg, var in kwargs.items():
            if not arg in input_args:
                continue

            if var is None:
                continue

            # send var to cuda
            if isinstance(var, dict):
                var = {k: v.cuda(self.gpu) for k, v in var.items()}
            else:
                var = var.cuda(self.gpu)
            input_dict[arg] = var
        return input_dict

    def train_step(self, *args, **kwargs):
        """
        train_step specific to each algorithm
        """
        # implement train step for each algorithm
        # compute loss
        # update model 
        # record tb_dict
        # return tb_dict
        raise NotImplementedError

    def train(self):
        """
        train function
        """
        self.model.train()
        self.call_hook("before_run")
        print('train_start')
        for epoch in range(self.epoch, self.epochs):
            self.epoch = epoch
            print('epoch',epoch,self.it,self.num_train_iter)
            #print('dataset[0]',self.loader_dict['train_lb'].dataset[0])
            #print('self.loader_dict',self.loader_dict['train_lb'].dataset[0],dir(self.loader_dict['train_lb']),len(self.loader_dict['train_lb']),len(self.loader_dict['train_ulb']))
            #for i in range(len(self.loader_dict['train_lb'])):
                #print("Inputs:", self.loader_dict['train_lb'][i])
                #print("Targets:", targets)
            # prevent the training iterations exceed args.num_train_iter
            if self.it >= self.num_train_iter:
                break

            self.call_hook("before_train_epoch")
            
            print('self.loader_dict',self.loader_dict['train_lb'],dir(self.loader_dict['train_lb']),len(self.loader_dict['train_lb']),len(self.loader_dict['train_ulb']))
            #print('self.loader_dict',self.loader_dict['train_lb'],dir(self.loader_dict['train_lb']),len(self.loader_dict['train_lb']),len(self.loader_dict['train_ulb']))
            #for inputs, targets in self.loader_dict['train_lb']:
                #print("Inputs:", inputs)
                #print("Targets:", targets)
            for data_lb, data_ulb in zip(self.loader_dict['train_lb'],
                                         self.loader_dict['train_ulb']):
                #print('data_lb',data_lb['x_lb'].shape,'data_ulb',data_ulb['x_ulb_w'].shape)
                # prevent the training iterations exceed args.num_train_iter
                if self.it >= self.num_train_iter:
                    break

                self.call_hook("before_train_step")
                self.tb_dict = self.train_step(**self.process_batch(**data_lb, **data_ulb))
                self.call_hook("after_train_step")
                self.it += 1

            self.call_hook("after_train_epoch")

        self.call_hook("after_run")

    def evaluate(self, eval_dest='eval', return_logits=False):
        """
        evaluation function
        """
        self.model.eval()
        self.ema.apply_shadow()
        eval_loader = self.loader_dict[eval_dest]
        total_loss = 0.0
        total_num = 0.0
        y_true = []
        y_pred = []
        # y_probs = []
        y_logits = []
        with torch.no_grad():
            for data in eval_loader:
                x = data['x_lb']
                y = data['y_lb']

                if isinstance(x, dict):
                    x = {k: v.cuda(self.gpu) for k, v in x.items()}
                else:
                    x = x.cuda(self.gpu)
                y = y.cuda(self.gpu)

                num_batch = y.shape[0]
                total_num += num_batch

                logits = self.model(x)['logits']

                loss = F.cross_entropy(logits, y, reduction='mean')
                y_true.extend(y.cpu().tolist())
                y_pred.extend(torch.max(logits, dim=-1)[1].cpu().tolist())
                y_logits.append(logits.cpu().numpy())
                # y_probs.append(torch.softmax(logits, dim=-1).cpu().numpy())
                total_loss += loss.item() * num_batch
        y_true = np.array(y_true)
        y_pred = np.array(y_pred)
        y_logits = np.concatenate(y_logits)
        top1 = accuracy_score(y_true, y_pred)
        # balanced_top1 = balanced_accuracy_score(y_true, y_pred)
        # precision = precision_score(y_true, y_pred, average='macro')
        # recall = recall_score(y_true, y_pred, average='macro')
        # F1 = f1_score(y_true, y_pred, average='macro')

        if self.num_classes <= 10:
            cf_mat = confusion_matrix(y_true, y_pred)
            self.print_fn('confusion matrix:\n' + np.array_str(cf_mat))
        self.ema.restore()
        self.model.train()

        # eval_dict = {eval_dest + '/loss': total_loss / total_num, eval_dest + '/top-1-acc': top1,
        #              eval_dest + '/balanced_acc': balanced_top1, eval_dest + '/precision': precision,
        #              eval_dest + '/recall': recall, eval_dest + '/F1': F1}

        eval_dict = {eval_dest + '/loss': total_loss / total_num, eval_dest + '/top-1-acc': top1}

        if return_logits:
            eval_dict[eval_dest + '/logits'] = y_logits
        return eval_dict

    def get_save_dict(self):
        """
        make easier for saving model when need save additional arguments
        """
        # base arguments for all models
        save_dict = {
            'model': self.model.state_dict(),
            'ema_model': self.ema_model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'scheduler': self.scheduler.state_dict(),
            'loss_scaler': self.loss_scaler.state_dict(),
            'epoch': self.epoch + 1,
            'it': self.it + 1,
            'best_it': self.best_it,
            'best_eval_acc': self.best_eval_acc,
        }
        return save_dict

    def save_model(self, save_name, save_path):
        """
        save model and specified parameters for resume
        """
        save_filename = os.path.join(save_path, save_name)
        save_dict = self.get_save_dict()
        torch.save(save_dict, save_filename)
        self.print_fn(f"model saved: {save_filename}")

    def load_model(self, load_path):
        """
        load model and specified parameters for resume
        """
        checkpoint = torch.load(load_path, map_location='cpu')
        self.model.load_state_dict(checkpoint['model'])
        self.ema_model.load_state_dict(checkpoint['ema_model'])
        self.optimizer.load_state_dict(checkpoint['optimizer'])
        self.scheduler.load_state_dict(checkpoint['scheduler'])
        self.loss_scaler.load_state_dict(checkpoint['loss_scaler'])
        self.epoch = checkpoint['epoch']
        self.it = checkpoint['it']
        self.best_it = checkpoint['best_it']
        self.best_eval_acc = checkpoint['best_eval_acc']
        self.print_fn('model loaded')
        return checkpoint

    def check_prefix_state_dict(self, state_dict):
        """
        remove prefix state dict in ema model
        """
        new_state_dict = dict()
        for key, item in state_dict.items():
            if key.startswith('module'):
                new_key = '.'.join(key.split('.')[1:])
            else:
                new_key = key
            new_state_dict[new_key] = item
        return new_state_dict

    def register_hook(self, hook, name=None, priority='LOWEST'):
        """
        Ref: https://github.com/open-mmlab/mmcv/blob/a08517790d26f8761910cac47ce8098faac7b627/mmcv/runner/base_runner.py#L263
        Register a hook into the hook list.
        The hook will be inserted into a priority queue, with the specified
        priority (See :class:`Priority` for details of priorities).
        For hooks with the same priority, they will be triggered in the same
        order as they are registered.
        Args:
            hook (:obj:`Hook`): The hook to be registered.
            name (:str, default to None): Name of the hook to be registered. Default is the hook class name.
            priority (int or str or :obj:`Priority`): Hook priority.
                Lower value means higher priority.
        """
        assert isinstance(hook, Hook)
        if hasattr(hook, 'priority'):
            raise ValueError('"priority" is a reserved attribute for hooks')
        priority = get_priority(priority)
        hook.priority = priority  # type: ignore
        hook.name = name if name is not None else type(hook).__name__

        # insert the hook to a sorted list
        inserted = False
        for i in range(len(self._hooks) - 1, -1, -1):
            if priority >= self._hooks[i].priority:  # type: ignore
                self._hooks.insert(i + 1, hook)
                inserted = True
                break

        if not inserted:
            self._hooks.insert(0, hook)

        # call set hooks
        self.hooks_dict = OrderedDict()
        for hook in self._hooks:
            self.hooks_dict[hook.name] = hook

    def call_hook(self, fn_name, hook_name=None, *args, **kwargs):
        """Call all hooks.
        Args:
            fn_name (str): The function name in each hook to be called, such as
                "before_train_epoch".
            hook_name (str): The specific hook name to be called, such as
                "param_update" or "dist_align", uesed to call single hook in train_step.
        """

        if hook_name is not None:
            return getattr(self.hooks_dict[hook_name], fn_name)(self, *args, **kwargs)

        for hook in self.hooks_dict.values():
            if hasattr(hook, fn_name):
                getattr(hook, fn_name)(self, *args, **kwargs)

    def registered_hook(self, hook_name):
        """
        Check if a hook is registered
        """
        return hook_name in self.hooks_dict

    @staticmethod
    def get_argument():
        """
        Get specificed arguments into argparse for each algorithm
        """
        return {}

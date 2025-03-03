"""Script for training the transoar project."""

import argparse
import os,sys
import random
from pathlib import Path
import warnings
warnings.filterwarnings("ignore", message="TypedStorage")
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
print("append to path & chdir:", base_dir)
os.chdir(base_dir)
sys.path.append(base_dir)
import numpy as np
import torch
import monai, re

from transoar.trainer_CL import Trainer_CL
from transoar.data.dataloader import get_loader
from transoar.utils.io import get_config, write_json, get_meta_data
# from transoar.models.transoarnet import TransoarNet
from transoar.models.organdetr_net import OrganDetrNet
from transoar.models.build import build_criterion

# def adapt_weights_old_to_new_v2(new_weights, old_weights):
#     """
#     Reduces old weights to match the new shape.
#     - Copies the first 16 channels from the checkpoint.
#     """
#     new_shape = new_weights.shape

#     if len(new_shape) == 1:  # Bias
#         new_weights = old_weights[:new_shape[0]]
#     elif len(new_shape) == 2:  # Linear layer (Classification head)
#         new_weights = old_weights[:new_shape[0], :]
#     elif len(new_shape) == 3: # 1D convolution weights
#         new_weights = old_weights[:new_shape[0], :, :]
#     elif len(new_shape) == 4: # 2D convolution weights
#         new_weights = old_weights[:new_shape[0], :, :, :]
#     elif len(new_shape) == 5:  # 3D convolution weights
#         new_weights = old_weights[:new_shape[0], :, :, :, :]
#     else:
#         raise ValueError("Unsupported tensor shape")

#     return new_weights

def adapt_weights_old_to_new(old_weights, new_weights):
    """
    Expands old weights to match the new shape.
    - Copies the first 16 channels from the checkpoint.
    - Randomly initializes the last 4 channels.
    """
    old_shape = old_weights.shape

    if len(old_shape) == 1:  # Bias
        new_weights[:old_shape[0]] = old_weights
    elif len(old_shape) == 2:  # Linear layer (Classification head)
        new_weights[:old_shape[0], :] = old_weights
    elif len(old_shape) == 3: # 1D convolution weights
        new_weights[:old_shape[0], :, :] = old_weights
    elif len(old_shape) == 4: # 2D convolution weights
        new_weights[:old_shape[0], :, :, :] = old_weights
    elif len(old_shape) == 5:  # 3D convolution weights
        new_weights[:old_shape[0], :, :, :, :] = old_weights
    else:
        raise ValueError("Unsupported tensor shape")

    return new_weights

def get_last_ckpt(filepath):
    """
    # check the best one
    keyword = 'model_best_'
    for f in os.listdir(filepath):
        if re.match(keyword, f):
            ckpt_file = f"{filepath}/{f}"
            return ckpt_file

    # check the other ckpts if avail
    keyword = 'model_epoch_'

    steps = []
    for f in os.listdir(filepath):
        if re.match(keyword, f):
            stepid = int(f.split(keyword)[-1].split('.pt')[0])
            steps.append(stepid)

    if steps:
        ckpt_file = f"{filepath}/{keyword}{max(steps)}.pt"
        return ckpt_file
    """

    # check if last checkpoint avail
    keyword = 'model_last.pt'
    ckpt_file = f"{filepath}/{keyword}"
    return ckpt_file




def match(n, keywords):
    out = False
    for b in keywords:
        if b in n:
            out = True
            break
    return out

def train(config, args):
    os.environ["CUDA_VISIBLE_DEVICES"] = config['device'][-1]
    device = 'cuda'

    # if device == 'cuda':
    #     # use bfloat16 for the entire notebook
    #     torch.autocast('cuda', dtype=torch.bfloat16).__enter__()
    #     # turn on tfloat32 for Ampere GPUs (https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices)
    #     if torch.cuda.get_device_properties(0).major >= 8:
    #         torch.backends.cuda.matmul.allow_tf32 = True
    #         torch.backends.cudnn.allow_tf32 = True

    # Build necessary components
    train_loader = get_loader(config, 'train')

    if config['overfit']:
        val_loader = get_loader(config, 'train')
    else:
        val_loader = get_loader(config, 'val')

    if config['test']:
        test_loader = get_loader(config, 'test') 
    else:
        test_loader = None
    
    # Load model from old model checkpoint
    model = OrganDetrNet(config).to(device=device)    

    if args.medicalnet:  # Download pretrained model from https://github.com/Tencent/MedicalNet
        assert config['backbone']['name'] == 'resnet', 'Loading MedicalNet is only possible if ResNet backbone is configured!'
        ckpt = torch.load('resnet_50.pth')
        fixed_ckpt = {}
        fixed_ckpt = {k.replace('module.',''): v for k, v in ckpt['state_dict'].items()}# checkpoint contains additional "module." prefix
        try:
            model._backbone.load_state_dict(fixed_ckpt, strict=True)
        except Exception as e:
            print("These layers are not loaded from the pretrained checkpoint: ", e)
            print("Loading pretrained model with strict=False ...")
            model._backbone.load_state_dict(fixed_ckpt, strict=False)
    elif args.pretrained_fpn:
        ckpt = torch.load('p224_bs3final_model.pth')
        fixed_ckpt = {}
        fixed_ckpt = {k.replace('attnFPN.',''): v for k, v in ckpt.items()}
        try:
            model._backbone.load_state_dict(fixed_ckpt, strict=True)
        except Exception as e:
            print("These layers are not loaded from the pretrained checkpoint: ", e)
            print("Loading pretrained model with strict=False ...")
            model._backbone.load_state_dict(fixed_ckpt, strict=False)

    if config.get('hybrid_dense_matching', False):
        criterion, dense_hybrid_criterion = build_criterion(config)
        criterion = criterion.to(device=device)
        dense_hybrid_criterion = dense_hybrid_criterion.to(device=device)
    else:
        criterion = build_criterion(config).to(device=device)
        dense_hybrid_criterion = None
    

    # Analysis of model parameter distribution
    num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    num_backbone_params = sum(p.numel() for n, p in model.named_parameters() if p.requires_grad and match(n, ['backbone', 'input_proj', 'skip']))
    num_neck_params = sum(p.numel() for n, p in model.named_parameters() if p.requires_grad and match(n, ['neck', 'query']))
    num_head_params = sum(p.numel() for n, p in model.named_parameters() if p.requires_grad and match(n, ['head']))

    param_dicts = [
        {
            'params': [
                p for n, p in model.named_parameters() if not match(n, ['backbone', 'reference_points', 'sampling_offsets']) and p.requires_grad
            ],
            'lr': float(config['lr'])
        },
        {
            'params': [p for n, p in model.named_parameters() if match(n, ['backbone']) and p.requires_grad],
            'lr': float(config['lr_backbone'])
        } 
    ]

    # Append additional param dict for def detr
    if sum([match(n, ['reference_points', 'sampling_offsets']) for n, _ in model.named_parameters()]) > 0:
        param_dicts.append(
            {
                "params": [
                    p for n, p in model.named_parameters() if match(n, ['reference_points', 'sampling_offsets']) and p.requires_grad
                ],
                'lr': float(config['lr']) * config['lr_linear_proj_mult']
            }
        )

    optim = torch.optim.AdamW(
        param_dicts, lr=float(config['lr_backbone']), weight_decay=float(config['weight_decay'])
    )
    scheduler = torch.optim.lr_scheduler.StepLR(optim, config['lr_drop'], config['lr_gamma'])


    # Init logging
    path_to_run = Path(os.getcwd()) / 'runs' / config['experiment_name']
    path_to_run.mkdir(exist_ok=True)

    # Start CL approach from old model if not mixing datasets training
    if config["mixing_datasets"] is False and config["CL"] is True:
        checkpoint_model = torch.load(config["CL_models"]["old_model_path"]) # Start main model with old model
        try:
            model.load_state_dict(checkpoint_model['model_state_dict'])
        except Exception as e:
            print("Warning: Some layers could not be loaded strictly. Adapting weights...")
            
            model_dict = model.state_dict()
            pretrained_dict = checkpoint_model['model_state_dict']
            updated_dict = {}
            
            for key, old_weights in pretrained_dict.items():
                if key in model_dict and old_weights.shape != model_dict[key].shape:
                    print(f"Adapting weights for layer: {key}")
                    new_weights = model_dict[key]
                    updated_dict[key] = adapt_weights_old_to_new(old_weights, new_weights)
                else:
                    updated_dict[key] = old_weights
            
            model_dict.update(updated_dict)
            model.load_state_dict(model_dict, strict=False)
            print("Model loaded with adapted weights.")
        print("Main model loaded.")

    # Load checkpoint if applicable
    if config.get('resume', False) or args.resume:
        ckpt_file = get_last_ckpt(path_to_run)
        print(f'[+] loading ckpt {ckpt_file} ...')
        checkpoint = torch.load(Path(ckpt_file))

        checkpoint['scheduler_state_dict']['step_size'] = config['lr_drop']

        # Unpack and load content
        try:
            model.load_state_dict(checkpoint['model_state_dict'])
        except Exception as e:
            print("Warning: Some layers could not be loaded strictly. Adapting weights...")
            
            model_dict = model.state_dict()
            pretrained_dict = checkpoint_model['model_state_dict']
            updated_dict = {}
            
            for key, old_weights in pretrained_dict.items():
                if key in model_dict and old_weights.shape != model_dict[key].shape:
                    print(f"Adapting weights for layer: {key}")
                    new_weights = model_dict[key]
                    updated_dict[key] = adapt_weights_old_to_new(old_weights, new_weights)
                else:
                    updated_dict[key] = old_weights
            
            model_dict.update(updated_dict)
            model.load_state_dict(model_dict, strict=False)
            print("Model loaded with adapted weights.")

        optim.load_state_dict(checkpoint['optimizer_state_dict'])
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
        epoch = checkpoint['epoch']
        metric_start_val = checkpoint['metric_max_val']

        if config['test']:
            try:
                metric_start_test = checkpoint['metric_max_test']
            except:
                metric_start_test = 0
        else:
            metric_start_test = 0
    else:
        epoch = 0
        metric_start_val = 0

        if config['test']:
            metric_start_test = 0
        else:
            metric_start_test = 0
        


    # log num_params
    num_params_dict ={'num_params': num_params,
                      'num_backbone_params': num_backbone_params,
                      'num_neck_params': num_neck_params,
                      'num_head_params': num_head_params
                      }
    config.update(num_params_dict)
    # Get meta data and write config to run
    try:
        config.update(get_meta_data())
    except:
        pass

    write_json(config, path_to_run / 'config.json')

    # Load auxiliary model and old model if applicable
    if config["mixing_datasets"] or config["CL_replay"] or config["CL"] is False or (config["CL"] is True and
        config["CL_models"]["aux_model_path"] is False or config["CL_models"]["old_model_path"] is False):
        aux_model = None
        old_model = None
    else:
        # Load auxiliary model from config["CL_models"]["aux_model_path"]
        aux_model = OrganDetrNet(config).to(device=device)
        checkpoint_aux_model = torch.load(config["CL_models"]["aux_model_path"])
        try:
            aux_model.load_state_dict(checkpoint_aux_model['model_state_dict'])
        except Exception as e:

            aux_model.load_state_dict(checkpoint_aux_model['model_state_dict'], strict=False)
            print("Aux Model loaded with adapted weights.")

        print("Auxiliary model loaded.")
        aux_model.eval()
        for param in aux_model.parameters():
            param.requires_grad = False

        # Load old model from config["CL_models"]["old_model_path"]
        old_model = OrganDetrNet(config).to(device=device)
        checkpoint_old_model = torch.load(config["CL_models"]["old_model_path"])
        try:
            old_model.load_state_dict(checkpoint_old_model['model_state_dict'])
        except Exception as e:
            print("Warning: Some layers could not be loaded strictly. Adapting weights...")
            
            model_dict = old_model.state_dict()
            pretrained_dict = checkpoint_model['model_state_dict']
            updated_dict = {}
            
            for key, old_weights in pretrained_dict.items():
                if key in model_dict and old_weights.shape != model_dict[key].shape:
                    print(f"Adapting weights for layer: {key}")
                    new_weights = model_dict[key]
                    updated_dict[key] = adapt_weights_old_to_new(old_weights, new_weights)
                else:
                    updated_dict[key] = old_weights
            
            model_dict.update(updated_dict)
            model.load_state_dict(model_dict, strict=False)

        print("Old model loaded.")
        old_model.eval()
        for param in old_model.parameters():
            param.requires_grad = False

    # Build trainer and start training
    trainer = Trainer_CL(
        train_loader, val_loader, test_loader, model, criterion, optim, scheduler, device, config, 
        path_to_run, epoch, metric_start_val, metric_start_test, dense_hybrid_criterion, aux_model, old_model
    )
    trainer.run()
        

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Add minimal amount of args (most args should be set in config files)
    parser.add_argument("--config", type=str, required=True, help="Config to use for training located in /config.")
    parser.add_argument("--resume", action='store_true', help="Auto-loads model_last.pt.")
    parser.add_argument("--medicalnet", action='store_true', help="Load pretrained ResNet")
    parser.add_argument("--pretrained_fpn", action='store_true', help="Load pretrained FPN")
    args = parser.parse_args()

    # Get relevant configs
    config = get_config(args.config)

    # To get reproducable results
    torch.manual_seed(config['seed'])
    np.random.seed(config['seed'])
    monai.utils.set_determinism(seed=config['seed'])
    random.seed(config['seed'])

    torch.backends.cudnn.benchmark = False  # performance vs. reproducibility
    torch.backends.cudnn.deterministic = True

    train(config, args)

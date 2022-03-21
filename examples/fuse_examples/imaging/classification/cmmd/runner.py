
"""

(C) Copyright 2021 IBM Corp.
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at
   http://www.apache.org/licenses/LICENSE-2.0
Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Created on June 30, 2021

"""

from collections import OrderedDict
import os
from fuse.eval.metrics.classification.metrics_thresholding_common import MetricApplyThresholds

from fuse.utils.utils_debug import FuseDebug
from fuse.utils.gpu import choose_and_enable_multiple_gpus

import logging

import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data.dataloader import DataLoader

from fuse.utils.utils_logger import fuse_logger_start
from fuse.utils import NDict
from fuse.data.utils.samplers import BatchSamplerDefault

from fuse.dl.models.model_default import ModelDefault
from fuse.dl.models.heads.head_global_pooling_classifier import HeadGlobalPoolingClassifier

from fuse.dl.losses.loss_default import LossDefault

from fuse.eval.metrics.classification.metrics_classification_common import MetricAUCROC, MetricAccuracy, MetricROCCurve

from fuse.dl.managers.callbacks.callback_tensorboard import TensorboardCallback
from fuse.dl.managers.callbacks.callback_metric_statistics import MetricStatisticsCallback
from fuse.dl.managers.callbacks.callback_time_statistics import TimeStatisticsCallback
from fuse.dl.managers.manager_default import ManagerDefault

from fuse_examples.imaging.classification.cmmd.dataset import CMMD_2021_dataset
from fuse.dl.models.backbones.backbone_inception_resnet_v2 import BackboneInceptionResnetV2

import hydra
from omegaconf import DictConfig, OmegaConf
from fuse.eval.evaluator import EvaluatorDefault
##########################################
# Debug modes
##########################################
mode = 'default'  # Options: 'default', 'fast', 'debug', 'verbose', 'user'. See details in FuseDebug
debug = FuseDebug(mode)


#################################
# Train Template
#################################
def run_train(paths : NDict , train: NDict ):
    # ==============================================================================
    # Logger
    # ==============================================================================
    fuse_logger_start(output_path=paths["model_dir"], console_verbose_level=logging.INFO)
    lgr = logging.getLogger('Fuse')

    # Download data
    # TBD

    lgr.info('\nFuse Train', {'attrs': ['bold', 'underline']})

    lgr.info(f'model_dir={paths["model_dir"]}', {'color': 'magenta'})
    lgr.info(f'cache_dir={paths["cache_dir"]}', {'color': 'magenta'})

    #### Train Data
    lgr.info(f'Train Data:', {'attrs': 'bold'})
    train_dataset, validation_dataset , _ = CMMD_2021_dataset(paths["data_dir"], paths["data_misc_dir"], reset_cache=train["reset_cache"])

    ## Create sampler
    lgr.info(f'- Create sampler:')
    sampler = BatchSamplerDefault(dataset=train_dataset,
                                       balanced_class_name='data.gt.classification',
                                       num_balanced_classes=2,
                                       batch_size=train["batch_size"],
                                       balanced_class_weights=None)

    lgr.info(f'- Create sampler: Done')

    ## Create dataloader
    train_dataloader = DataLoader(dataset=train_dataset,
                                  shuffle=False, drop_last=False,
                                  batch_sampler=sampler, collate_fn=train_dataset.collate_fn,
                                  num_workers=train["num_workers"])

    lgr.info(f'Train Data: Done', {'attrs': 'bold'})

    #### Validation data
    lgr.info(f'Validation Data:', {'attrs': 'bold'})




    ## Create dataloader
    validation_dataloader = DataLoader(dataset=validation_dataset,
                                       shuffle=False,
                                       drop_last=False,
                                       batch_sampler=None,
                                       batch_size=train["batch_size"],
                                       num_workers=train["num_workers"],
                                       collate_fn=validation_dataset.collate_fn)
    lgr.info(f'Validation Data: Done', {'attrs': 'bold'})

    # ===================================================================
    # ==============================================================================
    # Model
    # ==============================================================================
    lgr.info('Model:', {'attrs': 'bold'})

    model = ModelDefault(
        conv_inputs=(('data.input.image', 1),),
        backbone=BackboneInceptionResnetV2(input_channels_num=1),
        heads=[
            HeadGlobalPoolingClassifier(head_name='head_0',
                                            dropout_rate=0.5,
                                            conv_inputs=[('model.backbone_features', 384)],
                                            layers_description=(256,),
                                            num_classes=2,
                                            pooling="avg"),
        ]
    )

    lgr.info('Model: Done', {'attrs': 'bold'})


    # ====================================================================================
    #  Loss
    # ====================================================================================
    losses = {
        'cls_loss': LossDefault(pred='model.logits.head_0', target='data.gt.classification',
                                    callable=F.cross_entropy, weight=1.0)
    }


    # ====================================================================================
    # Metrics
    # ====================================================================================
    metrics = OrderedDict([
        ('op', MetricApplyThresholds(pred='model.output.head_0')), # will apply argmax
        ('auc', MetricAUCROC(pred='model.output.head_0', target='data.gt.classification')),
        ('accuracy', MetricAccuracy(pred='results:metrics.op.cls_pred', target='data.gt.classification')),
    ])

    # =====================================================================================
    #  Callbacks
    # =====================================================================================
    callbacks = [
        # default callbacks
        TensorboardCallback(model_dir=paths['model_dir']),  # save statistics for tensorboard
        MetricStatisticsCallback(output_path=paths['model_dir'] + "/metrics.csv"),  # save statistics in a csv file
        TimeStatisticsCallback(num_epochs=train["manager_train_params"]["num_epochs"], load_expected_part=0.1)  # time profiler
    ]


    # =====================================================================================
    #  Manager - Train
    #  Create a manager, training objects and run a training process.
    # =====================================================================================
    lgr.info('Train:', {'attrs': 'bold'})

    # create optimizer
    optimizer = optim.Adam(model.parameters(), lr=train["learning_rate"],
                           weight_decay=train["weight_decay"])

    # create scheduler
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer)

    # train from scratch

    manager = ManagerDefault(output_model_dir=paths['model_dir'], force_reset=paths['force_reset_model_dir'])

    # Providing the objects required for the training process.
    manager.set_objects(net=model,
                        optimizer=optimizer,
                        losses=losses,
                        metrics=metrics,
                        best_epoch_source=train["manager_best_epoch_source"],
                        lr_scheduler=scheduler,
                        callbacks=callbacks,
                        train_params=train["manager_train_params"],
                        output_model_dir=paths["model_dir"])

    # Continue training
    if train["resume_checkpoint_filename"] is not None:
        # Loading the checkpoint including model weights, learning rate, and epoch_index.
        manager.load_checkpoint(checkpoint=train["resume_checkpoint_filename"], mode='train',
                                values_to_resume=['net'])
    # # Start training
    manager.train(train_dataloader=train_dataloader,
                  validation_dataloader=validation_dataloader)

    lgr.info('Train: Done', {'attrs': 'bold'})

######################################
# Inference Template
######################################
def run_infer(paths : NDict , infer: NDict):
    #### Logger
    fuse_logger_start(output_path=paths["inference_dir"], console_verbose_level=logging.INFO)
    lgr = logging.getLogger('Fuse')
    lgr.info('Fuse Inference', {'attrs': ['bold', 'underline']})
    lgr.info(f'infer_filename={os.path.join(paths["inference_dir"], infer["infer_filename"])}', {'color': 'magenta'})

    # Create data source:
    _, _ , test_dataset = CMMD_2021_dataset(paths["data_dir"], paths["data_misc_dir"])


    ## Create dataloader
    infer_dataloader = DataLoader(dataset=test_dataset,
                                  shuffle=False, drop_last=False,
                                  collate_fn=test_dataset.collate_fn,
                                  num_workers=infer["num_workers"])
    lgr.info(f'Test Data: Done', {'attrs': 'bold'})

    #### Manager for inference
    manager = ManagerDefault()
    # extract just the global classification per sample and save to a file
    output_columns = ['model.output.head_0','data.gt.classification']
    manager.infer(data_loader=infer_dataloader,
                  input_model_dir=paths["model_dir"],
                  checkpoint=infer["checkpoint"],
                  output_columns=output_columns,
                  output_file_name=os.path.join(paths["inference_dir"], infer["infer_filename"]))
    


######################################
# Analyze Template
######################################
def run_eval(paths : NDict , infer: NDict):
    fuse_logger_start(output_path=None, console_verbose_level=logging.INFO)
    lgr = logging.getLogger('Fuse')
    lgr.info('Fuse Eval', {'attrs': ['bold', 'underline']})

     # metrics
    metrics = OrderedDict([
        ('op', MetricApplyThresholds(pred='model.output.head_0')), # will apply argmax
        ('auc', MetricAUCROC(pred='model.output.head_0', target='data.gt.classification')),
        ('accuracy', MetricAccuracy(pred='results:metrics.op.cls_pred', target='data.gt.classification')),
    ])

    # create evaluator
    evaluator = EvaluatorDefault()

    # run
    results = evaluator.eval(ids=None,
                     data=os.path.join(paths["inference_dir"], infer["infer_filename"]),
                     metrics=metrics,
                     output_dir=paths["eval_dir"])

    return results

@hydra.main(config_path="conf", config_name="config")
def main(cfg : DictConfig) -> None:
    cfg = NDict(OmegaConf.to_container(cfg))
    print(cfg)
    # uncomment if you want to use specific gpus instead of automatically looking for free ones
    force_gpus = None  # [0]
    choose_and_enable_multiple_gpus(cfg["train.manager_train_params.num_gpus"], force_gpus=force_gpus)


    RUNNING_MODES = ['train', 'infer', 'analyze']  # Options: 'train', 'infer', 'analyze'
    # Path to the stored dataset location
    # dataset should be download from https://wiki.cancerimagingarchive.net/pages/viewpage.action?pageId=70230508
    # download requires NBIA data retriever https://wiki.cancerimagingarchive.net/display/NBIA/Downloading+TCIA+Images
    # put on the folliwing in the main folder  - 
    # 1. CMMD_clinicaldata_revision.csv which is a converted version of CMMD_clinicaldata_revision.xlsx 
    # 2. folder named CMMD which is the downloaded data folder

    # train
    if 'train' in RUNNING_MODES:
        run_train(cfg["paths"] ,cfg["train"])

    # infer
    if 'infer' in RUNNING_MODES:
        run_infer(cfg["paths"] , cfg["infer"])
    #
    # analyze
    if 'analyze' in RUNNING_MODES:
        run_eval(cfg["paths"] ,cfg["infer"])
if __name__ == "__main__":
    main()

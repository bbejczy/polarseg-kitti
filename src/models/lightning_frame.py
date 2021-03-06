import os
import warnings
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn.functional as F
from torchlars import LARS

import src.misc.utils as utils
import wandb
from src.features.lovasz_losses import lovasz_softmax
from src.features.my_ptBEV import ptBEVnet

warnings.filterwarnings("ignore")


class PolarNetModule(pl.LightningModule):
    def __init__(self, config_name: str, out_sequence: Optional[Any] = None) -> None:
        super().__init__()

        # check if config path exists
        if Path("config/" + config_name).exists():
            self.config = utils.load_yaml("config/" + config_name)
        else:
            raise FileNotFoundError("Config file can not be found.")

        # load label information from semantic-kitti.yaml
        self.unique_class_idx, self.unique_class_name = utils.load_unique_classes(self.config["semkitti_config"])

        # define variables based on config file
        self.loss_function = torch.nn.CrossEntropyLoss(ignore_index=255)
        self.out_sequence = out_sequence
        self.model_name = Path(self.config["model_save_path"]).stem
        self.inference_path = "models/inference/{}/".format(self.model_name)

    def setup(self, stage: Optional[str] = None) -> None:
        # setup logging, except during inference (profiling)
        if stage == "test" or stage == "validate":
            self.config["logging"] = False
            self.profiling = True
        else:
            self.profiling = False

        # save configuration files to wandb
        if self.config["logging"]:
            wandb.config.update(self.config)

        # initialize model and pas configurations
        self.model = ptBEVnet(
            backbone_name=self.config["backbone"],
            grid_size=self.config["grid_size"],
            projection_type=self.config["projection_type"],
            n_class=self.unique_class_idx,
            circular_padding=self.config["augmentations"]["circular_padding"],
            sampling=self.config["sampling"],
            nine_feature = self.config["augmentations"]["9features"]
        )

        # for inference, load state_dict from the <model_name>.pt instance
        if stage == "validate" or stage == "test":
            if Path(self.config["model_save_path"]).exists():
                self.model.load_state_dict(torch.load(self.config["model_save_path"], map_location=self.device))
            else:
                raise FileExistsError("No trained model found.")

        # initialize evaluation metrics
        self.best_miou = 0
        self.epoch = 0

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.config["lr_rate"])
        if self.config["LARS"]:
            optimizer = LARS(optimizer=optimizer, eps=1e-8, trust_coef=0.001)
        return optimizer

    def validation_step(self, batch, batch_idx):

        # for timing the inference
        if self.profiling:
            start = torch.cuda.Event(enable_timing=True)
            end = torch.cuda.Event(enable_timing=True)

        # extract batch of numpy arrays
        vox_label, grid_index, pt_label, pt_features = batch

        # remap labels from 0->255 [based on documentation from semantic-kitti-api]
        vox_label = utils.move_labels(vox_label, -1)
        pt_label = utils.move_labels(pt_label, -1)

        # convert arrays to tensors
        grid_index_tensor = [torch.from_numpy(i[:, :2]).type(torch.IntTensor).to(self.device) for i in grid_index]
        pt_features_tensor = [torch.from_numpy(i).type(torch.FloatTensor).to(self.device) for i in pt_features]
        vox_label_tensor = torch.from_numpy(vox_label).type(torch.LongTensor).to(self.device)

        if self.profiling:
            start.record()

        # run inference through model
        prediction = self.model(pt_features_tensor, grid_index_tensor, self.device)

        if self.profiling:
            end.record()
            torch.cuda.synchronize()
            self.inference_time.append(start.elapsed_time(end))

        # CITATION: loss function [https://github.com/bermanmaxim/LovaszSoftmax]
        cross_entropy_loss = self.loss_function(prediction.detach(), vox_label_tensor)
        lovasz_loss = lovasz_softmax(F.softmax(prediction).detach(), vox_label_tensor, ignore=255)
        combined_loss = lovasz_loss + cross_entropy_loss
        # END OF CITATION: loss function

        # log validation loss
        if self.config["logging"]:
            wandb.log({"val_loss": combined_loss})
        self.val_loss_list.append(combined_loss.detach().cpu().numpy())

        prediction = torch.argmax(prediction, dim=1)
        prediction = prediction.detach().cpu().numpy()

        # generate confusion matrix from pointwise preditions and labels
        for i, __ in enumerate(grid_index):
            cm = utils.conf_mat_generator(
                prediction=prediction[i, grid_index[i][:, 0], grid_index[i][:, 1], grid_index[i][:, 2]].flatten(),
                label=pt_label[i].flatten(),
                classes=self.unique_class_idx,
                ignore_class=255,
            )
            self.confusion_matrix_sum = np.add(self.confusion_matrix_sum, cm)

    # executes at the beggining of every evaluation
    def on_validation_start(self):
        """
        Initialization of metric lists and confusion matrix.

        For inference: generate folder for test labels with the model name. Empty the folder if existing.
        """
        self.val_loss_list = []
        self.hist_list = []
        self.confusion_matrix_sum = np.zeros((len(self.unique_class_idx), len(self.unique_class_idx)), dtype=np.int32)
        if self.profiling:
            self.val_results_dict = {"model_params": sum(param.numel() for param in self.model.parameters())}
            self.inference_time = []
            utils.inference_dir(self.inference_path)

    # executes the per class iou calculations at the end of each validation block
    def on_validation_end(self):
        """
        Calculate per-class iou and overall miou from confusion matrix, log results to wandb.
        Save model state if the miou is better then before.

        Clean up inference time and write out results.
        """
        iou = utils.class_iou(self.confusion_matrix_sum)
        for class_name, class_iou in zip(self.unique_class_name, iou):
            if self.config["logging"]:
                wandb.log({f"{class_name}": class_iou})
            if self.profiling:
                self.val_results_dict.update({class_name: (class_iou)})
            print("%s : %.2f%%" % (class_name, class_iou))
        miou = np.nanmean(iou)

        # save model if performance is improved
        if self.best_miou < miou:
            self.best_miou = miou
            torch.save(self.model.state_dict(), self.config["model_save_path"])
        print("---\nCurrent validation miou: {:.4f}\nBest validation miou: {:.4f}".format(miou, self.best_miou))

        # log validation results to wandb
        if self.config["logging"]:
            wandb.log({"miou": miou, "best_miou": self.best_miou})

        # write inference results to file
        if self.profiling:
            self.val_results_dict.update({"best_miou": self.best_miou})
            self.val_results_dict.update({"inference": (np.mean(self.inference_time) * (1e-3))})
            utils.write_dict(
                self.val_results_dict, "models/inference/{}/results_{}.txt".format(self.model_name, self.model_name)
            )

    # initializations before new training
    def on_train_start(self) -> None:
        self.loss_list = []

    def on_train_epoch_start(self) -> None:
        # initialize epoch counter

        self.epoch += 1
        if self.config["logging"]:
            wandb.log({"epoch": self.epoch})

    def training_step(self, batch, batch_idx):

        vox_label, grid_index, pt_label, pt_features = batch

        # remap labels from 0->255
        vox_label = utils.move_labels(vox_label, -1)
        pt_label = utils.move_labels(pt_label, -1)

        grid_index_tensor = [torch.from_numpy(i[:, :2]).type(torch.IntTensor).to(self.device) for i in grid_index]
        pt_features_tensor = [torch.from_numpy(i).type(torch.FloatTensor).to(self.device) for i in pt_features]
        vox_label_tensor = torch.from_numpy(vox_label).type(torch.LongTensor).to(self.device)

        prediction = self.model(pt_features_tensor, grid_index_tensor, self.device)

        # CITATION: loss function [https://github.com/bermanmaxim/LovaszSoftmax]
        cross_entropy_loss = self.loss_function(prediction, vox_label_tensor)
        lovasz_loss = lovasz_softmax(F.softmax(prediction), vox_label_tensor, ignore=255)
        combined_loss = lovasz_loss + cross_entropy_loss
        # END OF CITATION: loss function

        if self.config["logging"]:
            wandb.log({"train_loss": combined_loss})
        self.loss_list.append(combined_loss.item())
        return combined_loss

    def test_step(self, batch, batch_idx):
        """
        Test loop, saving the predicted labels under the models/inference/<model_name>/sequences/ folder.
        """

        __, grid_index, __, pt_features, index = batch

        # identical to validation loop
        grid_index_tensor = [torch.from_numpy(i[:, :2]).type(torch.IntTensor).to(self.device) for i in grid_index]
        pt_features_tensor = [torch.from_numpy(i).type(torch.FloatTensor).to(self.device) for i in pt_features]

        # identical to validation loop
        prediction = self.model(pt_features_tensor, grid_index_tensor, self.device)
        prediction = (torch.argmax(prediction, 1)).cpu().detach().numpy()

        for i, __ in enumerate(grid_index):
            # process point-wise predition labels
            pt_pred_label = prediction[i, grid_index[i][:, 0], grid_index[i][:, 1], grid_index[i][:, 2]]
            pt_pred_label = (np.expand_dims(utils.move_labels(pt_pred_label, 1), axis=1)).astype(np.uint32)

            # find id and sequence for the of scan
            pt_id = Path(self.out_sequence.scan_list[index[i]]).stem
            sequence = Path(self.out_sequence.scan_list[index[i]]).parents[1].stem
            new_file_path = "{}sequences/{}/predictions/{}.label".format(
                self.inference_path, sequence, str(pt_id).zfill(6)
            )

            # write out label, if folder is valid
            if not Path(new_file_path).parents[0].exists():
                os.makedirs(Path(new_file_path).parents[0])
            pt_pred_label.tofile(new_file_path)

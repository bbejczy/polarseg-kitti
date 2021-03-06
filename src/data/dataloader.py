from pathlib import Path
from typing import Optional

import numpy as np
import pytorch_lightning as pl
from numba import jit
from torch.utils.data import DataLoader, Dataset

import src.misc.utils as utils


class PolarNetDataModule(pl.LightningDataModule):
    def __init__(self, config_name: str = "debug.yaml"):
        super().__init__()

        # load configuration file
        if Path("config/" + config_name).exists():
            self.config = utils.load_yaml("config/" + config_name)
        else:
            raise FileNotFoundError("Config file can not be found.")

        # define data folder
        if Path(self.config["data_dir"]).exists():
            self.data_dir = self.config["data_dir"]
        else:
            raise FileNotFoundError("Data folder can not be found.")

        # define path to semantic-kitti.yaml [source: semantic-kitti-api]
        self.semkitti_config = self.config["semkitti_config"]

        # failsafe for training logic
        assert self.config["projection_type"] in ["polar", "cartesian", "spherical"], "incorrect projection type"

    def setup(self, stage: Optional[str] = None) -> None:

        """
        initialization of dataset, stage is defined when calling with the trainer object
        """

        # initialization of pytroch dataset object: raw -> SemanticKITTI
        if stage == "fit" or stage is None:
            self.semkitti_train = SemanticKITTI(self.data_dir, data_split="train", semkitti_config=self.semkitti_config)
            self.semkitti_valid = SemanticKITTI(self.data_dir, data_split="valid", semkitti_config=self.semkitti_config)
        if stage == "validate" or stage is None:
            self.semkitti_valid = SemanticKITTI(self.data_dir, data_split="valid", semkitti_config=self.semkitti_config)
        if stage == "test" or stage is None:
            self.semkitti_test = SemanticKITTI(self.data_dir, data_split="test", semkitti_config=self.semkitti_config)

        # initialization of pytroch dataset object: SemanticKITTI -> voxelized
        if stage == "fit" or stage is None:
            self.voxelised_train = voxelised_dataset(self.config, self.semkitti_train, data_split="train")
            self.voxelised_valid = voxelised_dataset(self.config, self.semkitti_valid, data_split="valid")
        if stage == "validate" or stage is None:
            self.voxelised_valid = voxelised_dataset(self.config, self.semkitti_valid, data_split="valid")
        if stage == "test" or stage is None:
            self.voxelised_test = voxelised_dataset(self.config, self.semkitti_test, data_split="test")

    def train_dataloader(self):
        return DataLoader(
            self.voxelised_train,
            collate_fn=utils.collate_fn,
            shuffle=True,
            batch_size=self.config["train_batch"],
            num_workers=self.config["num_workers"],
        )

    def val_dataloader(self):
        return DataLoader(
            self.voxelised_valid,
            collate_fn=utils.collate_fn,
            shuffle=False,
            batch_size=self.config["valid_batch"],
            num_workers=self.config["num_workers"],
        )

    def test_dataloader(self):
        return DataLoader(
            self.voxelised_test,
            collate_fn=utils.collate_fn,
            shuffle=False,
            batch_size=self.config["test_batch"],
            num_workers=self.config["num_workers"],
        )


class SemanticKITTI(Dataset):
    """
    Loading Semantic KITTI dataset, from .bin and .label files.
    """

    def __init__(self, data_dir: str, data_split, semkitti_config) -> None:
        self.data_dir = data_dir
        self.semkitti_yaml = utils.load_yaml(semkitti_config)
        self.data_split = data_split
        self.scan_list = []
        self.label_list = []

        try:
            split = self.semkitti_yaml["split"][self.data_split]
        except ValueError:
            print("Incorrect set type")

        for sequence_folder in split:
            self.scan_list += utils.getPath(
                "/".join([self.data_dir, "sequences", str(sequence_folder).zfill(2), "velodyne"])
            )
            self.label_list += utils.getPath(
                "/".join([self.data_dir, "sequences", str(sequence_folder).zfill(2), "labels"])
            )

        self.scan_list.sort()
        self.label_list.sort()

    def __len__(self):
        return len(self.scan_list)

    def __getitem__(self, index):

        # scan is containing the (x,y,z, reflection)
        scan = np.fromfile(self.scan_list[index], dtype=np.float32).reshape(-1, 4)

        # labels prepared based on semkitti documentation
        if self.data_split == "test":
            labels = np.zeros(shape=scan[:, 0].shape, dtype=int)
        else:
            labels = np.fromfile(self.label_list[index], dtype=np.int32).reshape(-1, 1)
            labels = labels & 0xFFFF  # cut upper half of the binary [source: semantic-kitti-api]
            labels = utils.remap_labels(labels, self.semkitti_yaml).reshape(
                -1, 1
            )  # remap to cross-entropy labels [source: semantic-kitti-api]

        return (scan, labels)


class voxelised_dataset(Dataset):
    """
    Voxelization process of Semantic KITTI dataset, including augmentations and projection methods.

    Point feature variations, based on projection:

    1. Cartesian projection: ((x_offset, y_offset, z_offset), (x,y,z), reflection)
    2. Polar projection: ((rho_offset, theta_offset, z_offset), (rho, theta, z), (x, y), reflection)
    3. Polar projection [9features=False]: ((rho, theta), reflection)
    4. Sphercial projection: ((x_offset, y_offset, z_offset), (x,y,z), reflection)

    The residual distances (offset points) has been vaguely mentioned in the paper,
    therefore we have used the author's implementation from https://github.com/edwardzhou130/PolarSeg.

    After unsuccessful re-implementation, due to computational inefficiency, the voxel-label voting
    has been copied from https://github.com/edwardzhou130/PolarSeg
    """

    def __init__(
        self,
        config: dict,
        dataset,
        data_split,
    ):
        self.config = config
        self.dataset = dataset
        self.unlabeled_idx = utils.ignore_class(config["semkitti_config"])
        self.grid_size = np.asarray(config["grid_size"])
        self.max_vol = np.asarray(config["max_vol"], dtype=np.float32)
        self.min_vol = np.asarray(config["min_vol"], dtype=np.float32)
        self.data_split = data_split
        if self.config["projection_type"] == "spherical":
            self.proj_fov_up = 3.0
            self.proj_fov_down = -25.0
            self.proj_H = config["grid_size"][0]
            self.proj_W = config["grid_size"][1]
            self.proj_D = config["grid_size"][2]

    def __len__(self):
        return len(self.dataset)

    def __getitem__(self, index):

        # extract data
        data, labels = self.dataset[index]
        coordinate = data[:, :3]
        reflection = data[:, 3]

        # random flip
        if self.config["augmentations"]["flip"]:
            coordinate = utils.random_flip(coordinate)

        # random rotate
        if self.config["augmentations"]["rot"]:
            coordinate = utils.random_rot(coordinate)

        # change projection to polar
        if self.config["projection_type"] == "polar":
            coordinate_xy = coordinate[:, :2].copy()  # copy 2 cartesian coordinates for the 9features
            coordinate = utils.convert2polar(coordinate)

        # limit voxels to certain volume space
        if self.config["augmentations"]["fixed_vol"]:
            rebased_coordinate = utils.rebase(coordinate, self.min_vol, self.max_vol)
        else:
            rebased_coordinate = coordinate - self.min_vol

            self.max_vol = np.amax(coordinate, axis=0)
            self.min_vol = np.amin(coordinate, axis=0)

        if self.config["projection_type"] == "spherical":
            point_num = len(coordinate)

            # CITATION: calculating fov, depth, yaw and pitch from https://github.com/PRBonn/lidar-bonnetal/blob/master/train/common/laserscan.py
            fov_up = self.proj_fov_up / 180.0 * np.pi
            fov_down = self.proj_fov_down / 180.0 * np.pi
            fov = abs(fov_down) + abs(fov_up)

            depth = np.linalg.norm(coordinate, 2, axis=1)
            max_depth = np.floor(np.max(depth))
            min_depth = np.floor(np.min(depth))
            x, y, z = coordinate[:, 0], coordinate[:, 1], coordinate[:, 2]

            yaw = -np.arctan2(y, x)
            pitch = np.arcsin(z / depth)

            proj_w = (0.5 * (yaw / np.pi + 1.0)) * self.proj_W
            proj_h = (1.0 - (pitch + abs(fov_down)) / fov) * self.proj_H
            depth = depth.reshape(point_num, 1)

            proj_x_ind = np.floor(proj_h)
            proj_x_ind = np.minimum(self.proj_H - 1, proj_x_ind)
            proj_x_ind = np.maximum(0, proj_x_ind).astype(np.int32).reshape(point_num, 1)

            proj_y_ind = np.floor(proj_w)
            proj_y_ind = np.minimum(self.proj_W - 1, proj_y_ind)
            proj_y_ind = np.maximum(0, proj_y_ind).astype(np.int32).reshape(point_num, 1)

            grid_xy_ind = np.concatenate(([proj_x_ind, proj_y_ind]), axis=1)
            grid_z_ind = np.zeros(shape=(point_num, 1))
            for i in range(1, self.proj_D):
                grid_z_ind[depth > ((max_depth - min_depth) / self.proj_D) * i] = i + 1
            grid_index = np.concatenate(([grid_xy_ind, grid_z_ind]), axis=1).astype(np.int)

        else:
            # calculate the size of each voxel
            voxel_size = (self.max_vol - self.min_vol) / (self.grid_size - 1)

            # calculate the grid index for each point
            grid_index = np.floor(rebased_coordinate / voxel_size).astype(int)

        # CITATION: voxel-label voting from https://github.com/edwardzhou130/PolarSeg
        voxel_label = np.full(self.grid_size, self.unlabeled_idx, dtype=np.uint8)
        raw_point_label = np.concatenate([grid_index, labels.reshape(-1, 1)], axis=1)
        sorted_point_label = raw_point_label[
            np.lexsort((grid_index[:, 0], grid_index[:, 1], grid_index[:, 2])), :
        ].astype(np.int64)
        voxel_label = label_voting(np.copy(voxel_label), sorted_point_label)
        # END OF CITATION: voxel-label voting

        if self.config["projection_type"] == "spherical":
            proj_xy = np.concatenate((proj_h.reshape(point_num, 1), proj_w.reshape(point_num, 1)), axis=1)
            proj_xyz = np.concatenate((proj_xy, depth), axis=1)
            pt_features = np.concatenate((proj_xyz, coordinate, reflection.reshape(-1, 1)), axis=1)
        else:
            # CITATION: residual distances from https://github.com/edwardzhou130/PolarSeg
            voxel_center = (grid_index.astype(float) + 0.5) * voxel_size + self.min_vol
            centered_coordinate = coordinate - voxel_center
            # END OF CITATION: residual distances
            pt_features = np.concatenate((centered_coordinate, coordinate, reflection.reshape(-1, 1)), axis=1)

            if self.config["projection_type"] == "polar":
                if self.config["augmentations"]["9features"]:
                    pt_features = np.concatenate((pt_features, coordinate_xy), axis=1)
                else:
                    pt_features = np.concatenate((coordinate[:, :2], reflection.reshape(-1, 1)), axis=1)

        if self.data_split == "test":
            voxelised_data = (voxel_label, grid_index, labels, pt_features, index)
        else:
            voxelised_data = (voxel_label, grid_index, labels, pt_features)

        """
        *complete data_tuple*
        ---
        voxel_label: voxel-level label
        grid_index: individual point's grid index
        labels: individual point's label
        pt_features: [varies based on projection]
        [index]: only for testing
        """

        return voxelised_data


@jit("u1[:,:,:](u1[:,:,:],i8[:,:])", nopython=True, cache=True, parallel=False)
def label_voting(voxel_label: np.array, sorted_list: list):
    # CITATION: voxel-label voting from https://github.com/edwardzhou130/PolarSeg
    label_counter = np.zeros((256,), dtype=np.uint)
    label_counter[sorted_list[0, 3]] = 1
    compare_label_a = sorted_list[0, :3]
    for i in range(1, sorted_list.shape[0]):
        compare_label_b = sorted_list[i, :3]
        if not np.all(compare_label_a == compare_label_b):
            voxel_label[compare_label_a[0], compare_label_a[1], compare_label_a[2]] = np.argmax(label_counter)
            compare_label_a = compare_label_b
            label_counter = np.zeros((256,), dtype=np.uint)
        label_counter[sorted_list[i, 3]] += 1
    voxel_label[compare_label_a[0], compare_label_a[1], compare_label_a[2]] = np.argmax(label_counter)
    return voxel_label


def main():

    # debugging polar_datamodule
    data_module = PolarNetDataModule(config_name="debug.yaml")
    data_module.setup()

    dataloader = data_module.val_dataloader()

    for data in dataloader:
        print(data)


if __name__ == "__main__":
    main()

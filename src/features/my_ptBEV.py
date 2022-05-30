import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch_scatter
import torchvision.models as models

from src.features.my_BEV_Unet import Unet

class ptBEVnet(nn.Module):
    def __init__(
        self,
        backbone_name,
        grid_size,
        projection_type,
        n_class,
        out_pt_fea_dim=512,
        max_pt_per_encode=256,
        circular_padding=False,
    ):
        super(ptBEVnet, self).__init__()

        self.max_pt = max_pt_per_encode
        self.backbone_name = backbone_name
        self.n_height = grid_size[2]
        self.grid_size = grid_size
        self.n_class = len(n_class)
        self.circular_padding = circular_padding

        if projection_type == "traditional":
            fea_dim = 7
        elif projection_type == "polar":
            fea_dim = 9
        else:
            AssertionError, "incorrect projection type"

        self.PointNet = Simplified_PointNet(fea_dim, out_pt_fea_dim, self.n_height)

        assert self.backbone_name in ["UNet", "FCN", "DL"], "backbone name is incorrect"

        if self.backbone_name == "UNet":
            self.backbone = Unet(self.n_class, self.n_height, self.circular_padding)
            
        elif self.backbone_name == "FCN":
            self.backbone = models.segmentation.fcn_resnet50(pretrained=False, pretrained_backbone=False, num_classes=self.n_class*self.n_height)
            self.backbone.backbone.conv1 = nn.Conv2d(self.n_height, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False)
        
        elif self.backbone_name == "DL":
            self.backbone = models.segmentation.deeplabv3_resnet101(pretrained=False, pretrained_backbone=False, num_classes=self.n_class*self.n_height)
            self.backbone.backbone.conv1 = nn.Conv2d(self.n_height, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False)

    def forward(self, pt_fea, xy_ind, device):
        batch_size = len(pt_fea)

        fea, ind = [], []
        num = 0
        for i in range(batch_size):
            pt_num = len(pt_fea[i])
            batch_ind = torch.cat([torch.full((pt_num,1), i).to(device), xy_ind[i]], dim=1)
            fea.append(pt_fea[i])
            ind.append(batch_ind)
            num += pt_num
        fea, ind = torch.cat(fea, dim=0).to(device), torch.cat(ind, dim=0).to(device)
            
        random_ind = torch.randperm(num, device=device)
        fea, ind = torch.index_select(fea, dim=0, index=random_ind), torch.index_select(ind, dim=0, index=random_ind)
        unq, unq_inv, unq_cnt = torch.unique(ind, return_inverse=True, return_counts=True, dim=0)
        
        grp_ind = grp_range_torch(unq_cnt,device)[torch.argsort(torch.argsort(unq_inv))]
        remain_ind = grp_ind < self.max_pt
        fea = fea[remain_ind,:]
        ind = ind[remain_ind,:]
        unq_inv = unq_inv[remain_ind]
        unq_cnt = torch.clamp(unq_cnt,max=self.max_pt)

        pointnet_fea = self.PointNet(fea, unq, unq_inv, batch_size, device)

        if self.backbone_name == "UNet":
            backbone_fea = self.backbone(pointnet_fea)
        else:
            backbone_fea = self.backbone(pointnet_fea)
            backbone_fea = torch.tensor(backbone_fea['out']).to(device)
            class_per_voxel_dim = [batch_size, self.grid_size[0], self.grid_size[1], self.n_height, self.n_class]
            backbone_fea = backbone_fea.permute(0,2,3,1)
            backbone_fea = backbone_fea.view(class_per_voxel_dim).permute(0,4,1,2,3)
        return backbone_fea


class Simplified_PointNet(nn.Module):
    def __init__(self, fea_dim, out_pt_fea_dim, n_height, h, w):
        self.h, self.w, self.c = h, w, n_height
        
        self.PointNet = nn.Sequential(
            nn.BatchNorm1d(fea_dim),
            nn.Linear(fea_dim, 64),
            nn.BatchNorm1d(64),
            nn.LeakyReLU(inplace=True),
            nn.Linear(64, 128),
            nn.BatchNorm1d(128),
            nn.LeakyReLU(inplace=True),
            nn.Linear(128, 256),
            nn.BatchNorm1d(256),
            nn.LeakyReLU(inplace=True),
            nn.Linear(256, out_pt_fea_dim),
        )

        self.make_backbone_input_fea_dim = nn.Sequential(
            nn.Linear(out_pt_fea_dim, self.c), 
            nn.LeakyReLU()
        )

    def forward(self, fea, unq, unq_inv, n, device):
        backbone_input_dim = [n, self.h, self.w, self.c]
        backbone_data = torch.zeros(backbone_input_dim, dtype=torch.float32).to(device)
        pointnet_fea = self.PointNet(fea)
        max_pointnet_fea = torch_scatter.scatter_max(pointnet_fea, unq_inv, dim=0)[0]
        #max_pointnet_fea = []
        #for i in range(len(unq)):
        #    max_pointnet_fea.append(torch.max(pointnet_fea[unq_inv==i],dim=0)[0])
        #max_pointnet_fea = torch.stack(max_pointnet_fea)
        backbone_input_fea = self.make_backbone_input_fea_dim(max_pointnet_fea)
        backbone_data[unq[:,0],unq[:,1],unq[:,2],:] = backbone_input_fea
        return backbone_data.permute(0, 3, 1, 2)

def grp_range_torch(a,dev):
    idx = torch.cumsum(a,0)
    id_arr = torch.ones(idx[-1],dtype = torch.int64,device=dev)
    id_arr[0] = 0
    id_arr[idx[:-1]] = -a[:-1]+1
    return torch.cumsum(id_arr,0)

import torch
import torch.nn as nn
from collections import OrderedDict
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models import vgg16, vgg16_bn
from torchvision.models import resnet50

from config import Config
from dataset import class_labels_TR_sorted
from models.backbones.build_backbone import build_backbone
from models.modules.decoder_blocks import BasicDecBlk, ResBlk, HierarAttDecBlk
from models.modules.lateral_blocks import BasicLatBlk
from models.modules.aspp import ASPP, ASPPDeformable
from models.modules.ing import *
from models.refinement.refiner import Refiner, RefinerPVTInChannels4, RefUNet
from models.refinement.stem_layer import StemLayer


class BSL(nn.Module):
    def __init__(self):
        super(BSL, self).__init__()
        self.config = Config()
        self.epoch = 1
        self.bb = build_backbone(self.config.bb, pretrained=True)

        channels = self.config.lateral_channels_in_collection

        if self.config.auxiliary_classification:
            self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
            self.cls_head = nn.Sequential(
                nn.Linear(channels[0], len(class_labels_TR_sorted))
            )

        if self.config.squeeze_block:
            self.squeeze_module = nn.Sequential(*[
                eval(self.config.squeeze_block.split('_x')[0])(channels[0]+sum(self.config.cxt), channels[0])
                for _ in range(eval(self.config.squeeze_block.split('_x')[1]))
            ])

        self.decoder = Decoder(channels)
        
        if self.config.locate_head:
            self.locate_header = nn.ModuleList([
                BasicDecBlk(channels[0], channels[-1]),
                nn.Sequential(
                    nn.Conv2d(channels[-1], 1, 1, 1, 0),
                )
            ])

        if self.config.ender:
            self.dec_end = nn.Sequential(
                nn.Conv2d(1, 16, 3, 1, 1),
                nn.Conv2d(16, 1, 3, 1, 1),
                nn.ReLU(inplace=True),
            )

        # refine patch-level segmentation
        if self.config.refine:
            if self.config.refine == 'itself':
                self.stem_layer = StemLayer(in_channels=3+1, inter_channels=48, out_channels=3)
            else:
                self.refiner = eval('{}({})'.format(self.config.refine, 'in_channels=3+1'))

        if self.config.freeze_bb:
            # Freeze the backbone...
            print(self.named_parameters())
            for key, value in self.named_parameters():
                if 'bb.' in key and 'refiner.' not in key:
                    value.requires_grad = False

    def forward_enc(self, x):
        if self.config.bb in ['vgg16', 'vgg16bn', 'resnet50']:
            x1 = self.bb.conv1(x); x2 = self.bb.conv2(x1); x3 = self.bb.conv3(x2); x4 = self.bb.conv4(x3)
        else:
            x1, x2, x3, x4 = self.bb(x)
            if self.config.mul_scl_ipt == 'cat':
                B, C, H, W = x.shape
                x1_, x2_, x3_, x4_ = self.bb(F.interpolate(x, size=(H//2, W//2), mode='bilinear', align_corners=True))
                x1 = torch.cat([x1, F.interpolate(x1_, size=x1.shape[2:], mode='bilinear', align_corners=True)], dim=1)
                x2 = torch.cat([x2, F.interpolate(x2_, size=x2.shape[2:], mode='bilinear', align_corners=True)], dim=1)
                x3 = torch.cat([x3, F.interpolate(x3_, size=x3.shape[2:], mode='bilinear', align_corners=True)], dim=1)
                x4 = torch.cat([x4, F.interpolate(x4_, size=x4.shape[2:], mode='bilinear', align_corners=True)], dim=1)
            elif self.config.mul_scl_ipt == 'add':
                B, C, H, W = x.shape
                x1_, x2_, x3_, x4_ = self.bb(F.interpolate(x, size=(H//2, W//2), mode='bilinear', align_corners=True))
                x1 = x1 + F.interpolate(x1_, size=x1.shape[2:], mode='bilinear', align_corners=True)
                x2 = x2 + F.interpolate(x2_, size=x2.shape[2:], mode='bilinear', align_corners=True)
                x3 = x3 + F.interpolate(x3_, size=x3.shape[2:], mode='bilinear', align_corners=True)
                x4 = x4 + F.interpolate(x4_, size=x4.shape[2:], mode='bilinear', align_corners=True)
        class_preds = self.cls_head(self.avgpool(x4).view(x4.shape[0], -1)) if self.training and self.config.auxiliary_classification else None
        if self.config.cxt:
            x4 = torch.cat(
                (
                    *[
                        F.interpolate(x1, size=x4.shape[2:], mode='bilinear', align_corners=True),
                        F.interpolate(x2, size=x4.shape[2:], mode='bilinear', align_corners=True),
                        F.interpolate(x3, size=x4.shape[2:], mode='bilinear', align_corners=True),
                    ][-len(self.config.cxt):],
                    x4
                ),
                dim=1
            )
        return (x1, x2, x3, x4), class_preds

    # def forward_loc(self, x):
    #     ########## Encoder ##########
    #     (x1, x2, x3, x4), class_preds = self.forward_enc(x)
    #     if self.config.squeeze_block:
    #         x4 = self.squeeze_module(x4)
    #     if self.config.locate_head:
    #         locate_preds = self.locate_header[1](
    #             F.interpolate(
    #                 self.locate_header[0](
    #                     F.interpolate(x4, size=x2.shape[2:], mode='bilinear', align_corners=True)
    #                 ), size=x.shape[2:], mode='bilinear', align_corners=True
    #             )
    #         )

    def forward_ori(self, x):
        ########## Encoder ##########
        (x1, x2, x3, x4), class_preds = self.forward_enc(x)
        if self.config.squeeze_block:
            x4 = self.squeeze_module(x4)
        ########## Decoder ##########
        features = [x, x1, x2, x3, x4]
        scaled_preds = self.decoder(features)
        return scaled_preds, class_preds

    def forward_ref(self, x, pred):
        # refine patch-level segmentation
        if pred.shape[2:] != x.shape[2:]:
            pred = F.interpolate(pred, size=x.shape[2:], mode='bilinear', align_corners=True)
        # pred = pred.sigmoid()
        if self.config.refine == 'itself':
            x = self.stem_layer(torch.cat([x, pred], dim=1))
            scaled_preds, class_preds = self.forward_ori(x)
        else:
            scaled_preds = self.refiner([x, pred])
            class_preds = None
        return scaled_preds, class_preds

    def forward_ref_end(self, x):
        # remove the grids of concatenated preds
        return self.dec_end(x) if self.config.ender else x


    # def forward(self, x):
    #     if self.config.refine:
    #         scaled_preds, class_preds_ori = self.forward_ori(F.interpolate(x, size=(x.shape[2]//4, x.shape[3]//4), mode='bilinear', align_corners=True))
    #         class_preds_lst = [class_preds_ori]
    #         for _ in range(self.config.refine_iteration):
    #             scaled_preds_ref, class_preds_ref = self.forward_ref(x, scaled_preds[-1])
    #             scaled_preds += scaled_preds_ref
    #             class_preds_lst.append(class_preds_ref)
    #     else:
    #         scaled_preds, class_preds = self.forward_ori(x)
    #         class_preds_lst = [class_preds]
    #     return [scaled_preds, class_preds_lst] if self.training else scaled_preds

    def forward(self, x):
        if self.config.refine:
            if self.config.progressive_ref:
                scale = self.config.scale
                scaled_preds, class_preds_ori = self.forward_ori(
                    F.interpolate(x, size=(x.shape[2]//scale, x.shape[3]//scale), mode='bilinear', align_corners=True)
                )
                class_preds_lst = [class_preds_ori]
                for _ in range(self.config.refine_iteration):
                    _size_w, _size_h = x.shape[2] // scale, x.shape[3] // scale
                    x_lst, pred_lst = [], []
                    y = F.interpolate(
                        scaled_preds[-1],
                        size=(x.shape[2], x.shape[3]),
                        mode='bilinear',
                        align_corners=True
                    )
                    for idx in range(x.shape[0]):
                        columns_x = torch.split(x[idx], split_size_or_sections=_size_w, dim=-1)
                        columns_pred = torch.split(y[idx], split_size_or_sections=_size_w, dim=-1)
                        patches_x, patches_pred = [], []
                        for column_x in columns_x:
                            patches_x += [p.unsqueeze(0) for p in torch.split(column_x, split_size_or_sections=_size_h, dim=-2)]
                        for column_pred in columns_pred:
                            patches_pred += [p.unsqueeze(0) for p in torch.split(column_pred, split_size_or_sections=_size_h, dim=-2)]
                        x_lst += patches_x
                        pred_lst += patches_pred
                    scaled_preds_ref, class_preds_ref = self.forward_ref(
                        torch.cat(x_lst, dim=0),
                        torch.cat(pred_lst, dim=0),
                    )
                    scaled_preds_ref_recovered = []
                    for idx_end_of_sample in range(0, (self.config.batch_size if self.training else self.config.batch_size_valid)*(scale**2), scale**2):
                        preds_one_sample = scaled_preds_ref[-1][idx_end_of_sample:idx_end_of_sample+scale**2]
                        one_sample = []
                        for idx_pred in range(preds_one_sample.shape[0]):
                            if idx_pred % scale == 0:
                                one_column = []
                            one_column.append(preds_one_sample[idx_pred])
                            if len(one_column) == scale:
                                one_sample.append(torch.cat(one_column, dim=-2))
                        one_sample = torch.cat(one_sample, dim=-1)
                        scaled_preds_ref_recovered.append(one_sample.unsqueeze(0))
                    scaled_preds_ref_recovered_cat = torch.cat(scaled_preds_ref_recovered, dim=0)
                    if self.config.ender:
                        scaled_preds_ref_recovered_cat = self.forward_ref_end(scaled_preds_ref_recovered_cat)
                    scaled_preds.append(scaled_preds_ref_recovered_cat)
                    # class_preds_lst.append(class_preds_ref)
            else:
                scaled_preds, class_preds_ori = self.forward_ori(x)
                class_preds_lst = [class_preds_ori]
                for _ in range(self.config.refine_iteration):
                    scaled_preds_ref, class_preds_ref = self.forward_ref(x, scaled_preds[-1])
                    scaled_preds += scaled_preds_ref
                    class_preds_lst.append(class_preds_ref)
        else:
            scaled_preds, class_preds = self.forward_ori(x)
            class_preds_lst = [class_preds]
        return [scaled_preds, class_preds_lst] if self.training else scaled_preds


class Decoder(nn.Module):
    def __init__(self, channels):
        super(Decoder, self).__init__()
        self.config = Config()
        DecoderBlock = eval(self.config.dec_blk)
        LateralBlock = eval(self.config.lat_blk)

        if self.config.dec_ipt:
            self.split = self.config.dec_ipt_split
            N_dec_ipt = 64
            DBlock = InceptionC
            ic = 16
            ipt_cha_opt = 1
            self.ipt_blk4 = DBlock(2**8*3 if self.split else 3, [N_dec_ipt, channels[0]//8][ipt_cha_opt], inter_channels=ic)
            self.ipt_blk3 = DBlock(2**6*3 if self.split else 3, [N_dec_ipt, channels[1]//8][ipt_cha_opt], inter_channels=ic)
            self.ipt_blk2 = DBlock(2**4*3 if self.split else 3, [N_dec_ipt, channels[2]//8][ipt_cha_opt], inter_channels=ic)
            self.ipt_blk1 = DBlock(2**0*3 if self.split else 3, [N_dec_ipt, channels[3]//8][ipt_cha_opt], inter_channels=ic)
        else:
            self.split = None

        self.decoder_block4 = DecoderBlock(channels[0], channels[1])
        self.decoder_block3 = DecoderBlock(channels[1]+([N_dec_ipt, channels[0]//8][ipt_cha_opt] if self.config.dec_ipt else 0), channels[2])
        self.decoder_block2 = DecoderBlock(channels[2]+([N_dec_ipt, channels[1]//8][ipt_cha_opt] if self.config.dec_ipt else 0), channels[3])
        self.decoder_block1 = DecoderBlock(channels[3]+([N_dec_ipt, channels[2]//8][ipt_cha_opt] if self.config.dec_ipt else 0), channels[3]//2)
        self.conv_out1 = nn.Sequential(nn.Conv2d(channels[3]//2+([N_dec_ipt, channels[3]//8][ipt_cha_opt] if self.config.dec_ipt else 0), 1, 1, 1, 0))

        self.lateral_block4 = LateralBlock(channels[1], channels[1])
        self.lateral_block3 = LateralBlock(channels[2], channels[2])
        self.lateral_block2 = LateralBlock(channels[3], channels[3])

        if self.config.ms_supervision:
            self.conv_ms_spvn_4 = nn.Conv2d(channels[1], 1, 1, 1, 0)
            self.conv_ms_spvn_3 = nn.Conv2d(channels[2], 1, 1, 1, 0)
            self.conv_ms_spvn_2 = nn.Conv2d(channels[3], 1, 1, 1, 0)


    def get_patches_batch(self, x, p):
        _size_h, _size_w = p.shape[2:]
        patches_batch = []
        for idx in range(x.shape[0]):
            columns_x = torch.split(x[idx], split_size_or_sections=_size_w, dim=-1)
            patches_x = []
            for column_x in columns_x:
                patches_x += [p.unsqueeze(0) for p in torch.split(column_x, split_size_or_sections=_size_h, dim=-2)]
            patch_sample = torch.cat(patches_x, dim=1)
            patches_batch.append(patch_sample)
        return torch.cat(patches_batch, dim=0)

    def forward(self, features):
        x, x1, x2, x3, x4 = features
        outs = []
        p4 = self.decoder_block4(x4)
        _p4 = F.interpolate(p4, size=x3.shape[2:], mode='bilinear', align_corners=True)
        _p3 = _p4 + self.lateral_block4(x3)
        if self.config.dec_ipt:
            patches_batch = self.get_patches_batch(x, _p3) if self.split else x
            _p3 = torch.cat((_p3, self.ipt_blk4(F.interpolate(patches_batch, size=x3.shape[2:], mode='bilinear', align_corners=True))), 1)

        p3 = self.decoder_block3(_p3)
        _p3 = F.interpolate(p3, size=x2.shape[2:], mode='bilinear', align_corners=True)
        _p2 = _p3 + self.lateral_block3(x2)
        if self.config.dec_ipt:
            patches_batch = self.get_patches_batch(x, _p2) if self.split else x
            _p2 = torch.cat((_p2, self.ipt_blk3(F.interpolate(patches_batch, size=x2.shape[2:], mode='bilinear', align_corners=True))), 1)

        p2 = self.decoder_block2(_p2)
        _p2 = F.interpolate(p2, size=x1.shape[2:], mode='bilinear', align_corners=True)
        _p1 = _p2 + self.lateral_block2(x1)
        if self.config.dec_ipt:
            patches_batch = self.get_patches_batch(x, _p1) if self.split else x
            _p1 = torch.cat((_p1, self.ipt_blk2(F.interpolate(patches_batch, size=x1.shape[2:], mode='bilinear', align_corners=True))), 1)

        _p1 = self.decoder_block1(_p1)
        _p1 = F.interpolate(_p1, size=x.shape[2:], mode='bilinear', align_corners=True)
        if self.config.dec_ipt:
            patches_batch = self.get_patches_batch(x, _p1) if self.split else x
            _p1 = torch.cat((_p1, self.ipt_blk1(F.interpolate(patches_batch, size=x.shape[2:], mode='bilinear', align_corners=True))), 1)
        p1_out = self.conv_out1(_p1)

        if self.config.ms_supervision:
            outs.append(self.conv_ms_spvn_4(p4))
            outs.append(self.conv_ms_spvn_3(p3))
            outs.append(self.conv_ms_spvn_2(p2))
        outs.append(p1_out)
        return outs


class AAA(nn.Module):
    def __init__(
        self, in_channels: int, out_channels: int, inter_channels=64
    ) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(in_channels, inter_channels, 3, 1, 1)
        self.conv_out = nn.Conv2d(inter_channels, out_channels, 3, 1, 1)

    def forward(self, x):
        return self.conv_out(self.conv1(x))


class InceptionC(nn.Module):
    def __init__(
        self, in_channels: int, out_channels: int, inter_channels=16, conv_block=None
    ) -> None:
        super().__init__()
        if conv_block is None:
            conv_block = torch.nn.Conv2d
        self.branch1x1 = conv_block(in_channels, int(out_channels//4), kernel_size=1)

        c7 = inter_channels
        self.branch7x7_1 = conv_block(in_channels, c7, kernel_size=1)
        self.branch7x7_2 = conv_block(c7, c7, kernel_size=(1, 7), padding=(0, 3))
        self.branch7x7_3 = conv_block(c7, int(out_channels//4), kernel_size=(7, 1), padding=(3, 0))

        self.branch7x7dbl_1 = conv_block(in_channels, c7, kernel_size=1)
        self.branch7x7dbl_2 = conv_block(c7, c7, kernel_size=(7, 1), padding=(3, 0))
        self.branch7x7dbl_3 = conv_block(c7, c7, kernel_size=(1, 7), padding=(0, 3))
        self.branch7x7dbl_4 = conv_block(c7, c7, kernel_size=(7, 1), padding=(3, 0))
        self.branch7x7dbl_5 = conv_block(c7, int(out_channels//4), kernel_size=(1, 7), padding=(0, 3))

        self.branch_pool = conv_block(in_channels, int(out_channels//4), kernel_size=1)
        self.conv_out = conv_block(int(out_channels//4)*4, out_channels, 1, 1, 0)

    def _forward(self, x):
        branch1x1 = self.branch1x1(x)

        branch7x7 = self.branch7x7_1(x)
        branch7x7 = self.branch7x7_2(branch7x7)
        branch7x7 = self.branch7x7_3(branch7x7)

        branch7x7dbl = self.branch7x7dbl_1(x)
        branch7x7dbl = self.branch7x7dbl_2(branch7x7dbl)
        branch7x7dbl = self.branch7x7dbl_3(branch7x7dbl)
        branch7x7dbl = self.branch7x7dbl_4(branch7x7dbl)
        branch7x7dbl = self.branch7x7dbl_5(branch7x7dbl)

        branch_pool = F.avg_pool2d(x, kernel_size=3, stride=1, padding=1)
        branch_pool = self.branch_pool(branch_pool)

        outputs = [branch1x1, branch7x7, branch7x7dbl, branch_pool]
        return outputs

    def forward(self, x):
        outputs = self._forward(x)
        return self.conv_out(torch.cat(outputs, 1))

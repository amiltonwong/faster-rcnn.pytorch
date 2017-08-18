# --------------------------------------------------------
# Faster R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick and Sean Bell
# --------------------------------------------------------
# --------------------------------------------------------
# Reorganized and modified by Jianwei Yang and Jiasen Lu
# --------------------------------------------------------

import torch
import torch.nn as nn
import numpy as np
import numpy.random as npr
from ..utils.config import cfg
from bbox_transform import bbox_transform, bbox_overlaps, bbox_overlaps_batch2, bbox_transform_batch2
import pdb

DEBUG = False

class _ProposalTargetLayer(nn.Module):
    """
    Assign object detection proposals to ground-truth targets. Produces proposal
    classification labels and bounding-box regression targets.
    """

    def __init__(self, nclasses):
        super(_ProposalTargetLayer, self).__init__()
        self._num_classes = nclasses
        self.BBOX_NORMALIZE_MEANS = torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS)
        self.BBOX_NORMALIZE_STDS = torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS)
        self.BBOX_INSIDE_WEIGHTS = torch.FloatTensor(cfg.TRAIN.BBOX_INSIDE_WEIGHTS)

    def forward(self, all_rois, gt_boxes, num_boxes):
        # Proposal ROIs (0, x1, y1, x2, y2) coming from RPN
        # (i.e., rpn.proposal_layer.ProposalLayer), or any other source
        # all_rois = bottom[0].data
        # GT boxes (x1, y1, x2, y2, label)
        # TODO(rbg): it's annoying that sometimes I have extra info before
        # and other times after box coordinates -- normalize to one format
        # gt_boxes = bottom[1].data

        # Include ground-truth boxes in the set of candidate rois
        #all_rois_np = all_rois.numpy()
        #gt_boxes_np = gt_boxes.numpy()

        #zeros = np.zeros((gt_boxes_np.shape[0], 1), dtype=gt_boxes_np.dtype)
        #all_rois_np = np.vstack(
        #    (all_rois_np, np.hstack((zeros, gt_boxes_np[:, :-1])))
        #)
        
        self.BBOX_NORMALIZE_MEANS = self.BBOX_NORMALIZE_MEANS.type_as(gt_boxes)
        self.BBOX_NORMALIZE_STDS = self.BBOX_NORMALIZE_STDS.type_as(gt_boxes)
        self.BBOX_INSIDE_WEIGHTS = self.BBOX_INSIDE_WEIGHTS.type_as(gt_boxes)

        gt_boxes_append = gt_boxes.new(gt_boxes.size()).zero_()
        gt_boxes_append[:,:,1:5] = gt_boxes[:,:,:4]

        # Include ground-truth boxes in the set of candidate rois
        all_rois = torch.cat([all_rois, gt_boxes_append], 1)

        # Sanity check: single batch only
        # TODO: determined by GPU number
        # assert np.all(all_rois[:, 0] == 0), \
        #         'Only single item batches are supported'

        num_images = 1
        rois_per_image = int(cfg.TRAIN.BATCH_SIZE / num_images)
        fg_rois_per_image = int(np.round(cfg.TRAIN.FG_FRACTION * rois_per_image))

        # Sample rois with classification labels and bounding box regression
        # targets

        # labels_old, rois_old, bbox_targets, bbox_inside_weights = _sample_rois(
        #     all_rois_np, gt_boxes_np, fg_rois_per_image,
        #     rois_per_image, self._num_classes)

        labels, rois, bbox_targets, bbox_inside_weights = self._sample_rois_pytorch(
            all_rois, gt_boxes, fg_rois_per_image,
            rois_per_image, self._num_classes)

        # rois = torch.from_numpy(rois.reshape(-1, 5))
        # labels = torch.from_numpy(labels.reshape(-1, 1))
        # bbox_targets = torch.from_numpy(bbox_targets.reshape(-1, self._num_classes * 4))
        # bbox_inside_weights = torch.from_numpy(bbox_inside_weights.reshape(-1, self._num_classes * 4))
        # bbox_outside_weights = (bbox_inside_weights > 0).float()
        # torch.from_numpy(np.array(bbox_inside_weights > 0).astype(np.float32))
        # bbox_outside_weights = (bbox_inside_weights > 0).float()

        return rois, labels, bbox_targets, bbox_inside_weights, 

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass

    def _get_bbox_regression_labels_pytorch(self, bbox_target_data, labels_batch, num_classes):
        """Bounding-box regression targets (bbox_target_data) are stored in a
        compact form b x N x (class, tx, ty, tw, th)

        This function expands those targets into the 4-of-4*K representation used
        by the network (i.e. only one class has non-zero targets).

        Returns:
            bbox_target (ndarray): b x N x 4K blob of regression targets
            bbox_inside_weights (ndarray): b x N x 4K blob of loss weights
        """

        batch_size = labels_batch.size(0)
        rois_per_image = labels_batch.size(1)
        clss = labels_batch
        bbox_targets = bbox_target_data.new(batch_size, rois_per_image, 4*num_classes).zero_()
        bbox_inside_weights = bbox_target_data.new(bbox_targets.size()).zero_()
        
        for b in range(batch_size):
            inds = torch.nonzero(clss[b] > 0).squeeze()
            for i in range(inds.numel()):
                ind = inds[i]
                cls = clss[b, ind]
                start = int(4 * cls)
                end = start + 4
                bbox_targets[b, ind, start:end] = bbox_target_data[b, ind, :]
                bbox_inside_weights[b, ind, start:end] = self.BBOX_INSIDE_WEIGHTS

        return bbox_targets, bbox_inside_weights


    def _compute_targets_pytorch(self, ex_rois, gt_rois):
        """Compute bounding-box regression targets for an image."""

        assert ex_rois.size(1) == gt_rois.size(1)
        assert ex_rois.size(2) == 4
        assert gt_rois.size(2) == 4

        batch_size = ex_rois.size(0)
        rois_per_image = ex_rois.size(1)

        targets = bbox_transform_batch2(ex_rois, gt_rois)

        if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
            # Optionally normalize targets by a precomputed mean and stdev
            targets = ((targets - self.BBOX_NORMALIZE_MEANS.expand_as(targets))
                        / self.BBOX_NORMALIZE_STDS.expand_as(targets))

        return targets


    def _sample_rois_pytorch(self, all_rois, gt_boxes, fg_rois_per_image, rois_per_image, num_classes):
        """Generate a random sample of RoIs comprising foreground and background
        examples.
        """
        # overlaps: (rois x gt_boxes)

        overlaps, all_rois_zero, gt_boxes_zero = bbox_overlaps_batch2(all_rois, gt_boxes)

        max_overlaps, gt_assignment = torch.max(overlaps, 2)

        batch_size = overlaps.size(0)
        num_proposal = overlaps.size(1)
        num_boxes_per_img = overlaps.size(2)

        offset = torch.arange(0, batch_size)*20
        offset = offset.view(-1, 1).type_as(gt_assignment) + gt_assignment

        labels = gt_boxes[:,:,4].contiguous().view(-1).index(offset.view(-1))\
                                                            .view(batch_size, -1)

        # Those labels may contains 0, which is wrongly assigned to padding grouding truth
        # bounding box. we need to filter them out by assign them -1. 
        # labels.masked_fill_(all_rois_zero, -1)


        # Select foreground RoIs as those with >= FG_THRESH overlap
        # fg_inds = np.where(max_overlaps >= cfg.TRAIN.FG_THRESH)[0]
        fg_mask = max_overlaps >= cfg.TRAIN.FG_THRESH

        labels_batch = labels.new(batch_size, rois_per_image).zero_()
        rois_batch  = all_rois.new(batch_size, rois_per_image, 5).zero_()
        gt_rois_batch = all_rois.new(batch_size, rois_per_image, 5).zero_()
        # Guard against the case when an image has fewer than fg_rois_per_image
        # foreground RoIs
        for i in range(batch_size):
            fg_inds = torch.nonzero(max_overlaps[i] >= cfg.TRAIN.FG_THRESH).squeeze()
            fg_num_rois = fg_inds.numel()
            fg_rois_per_this_image = min(fg_rois_per_image, fg_num_rois)
            # Sample foreground regions without replacement
            if fg_num_rois > 0:            
                rand_num = torch.randperm(fg_num_rois).type_as(all_rois).long()
                fg_inds = fg_inds[rand_num[:fg_rois_per_image]]

            # Select background RoIs as those within [BG_THRESH_LO, BG_THRESH_HI)
            bg_inds = torch.nonzero((max_overlaps[i] < cfg.TRAIN.BG_THRESH_HI) &
                                    (max_overlaps[i] >= cfg.TRAIN.BG_THRESH_LO)).squeeze()

            bg_num_rois = bg_inds.numel()
            # Compute number of background RoIs to take from this image (guarding
            # against there being fewer than desired)
            bg_rois_per_this_image = rois_per_image - fg_rois_per_this_image
            bg_rois_per_this_image = min(bg_rois_per_this_image, bg_inds.size)
            # Sample background regions without replacement
            if bg_num_rois > 0:
                rand_num = torch.randperm(bg_num_rois).type_as(all_rois).long()
                bg_inds = bg_inds[rand_num[:bg_rois_per_this_image]]

            # The indices that we're selecting (both fg and bg)
            keep_inds = torch.cat([fg_inds, bg_inds], 0)
        
            # Select sampled values from various arrays:
            labels_batch[i].copy_(labels[i][keep_inds])
            
            # Clamp labels for the background RoIs to 0
            labels_batch[i][fg_rois_per_this_image:] = 0
            rois_batch[i].copy_(all_rois[i][keep_inds])
            rois_batch[i,:,0] = i

            gt_rois_batch[i].copy_(gt_boxes[i][gt_assignment[i][keep_inds]])


        bbox_target_data = self._compute_targets_pytorch(
                rois_batch[:,:,1:5], gt_rois_batch[:,:,:4])

        bbox_targets, bbox_inside_weights = \
                self._get_bbox_regression_labels_pytorch(bbox_target_data, labels_batch, num_classes)


        return labels_batch, rois_batch, bbox_targets, bbox_inside_weights



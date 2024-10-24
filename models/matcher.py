# ------------------------------------------------------------------------
# Modified from Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
Modules to compute the matching cost and solve the corresponding LSAP.
"""
import torch
from scipy.optimize import linear_sum_assignment
from torch import nn

from util.box_ops import box_cxcywh_to_xyxy, generalized_box_iou


class HungarianMatcher(nn.Module):
    """This class computes an assignment between the targets and the predictions of the network

    For efficiency reasons, the targets don't include the no_object. Because of this, in general,
    there are more predictions than targets. In this case, we do a 1-to-1 matching of the best predictions,
    while the others are un-matched (and thus treated as non-objects).
    """

    def __init__(self, cost_class: float = 1, cost_bbox: float = 1, cost_giou: float = 1, cost_embed: float = 0.5):
        """Creates the matcher

        Params:
            cost_class: This is the relative weight of the classification error in the matching cost
            cost_bbox: This is the relative weight of the L1 error of the bounding box coordinates in the matching cost
            cost_giou: This is the relative weight of the giou loss of the bounding box in the matching cost
            cost_embed: Relative weight of the embedding distance cost in the matching
        """
        super().__init__()
        self.cost_class = cost_class
        self.cost_bbox = cost_bbox
        self.cost_giou = cost_giou
        self.cost_embed = cost_embed
        assert cost_class != 0 or cost_bbox != 0 or cost_giou != 0 or cost_embed != 0, "all costs can't be 0"

    def forward(self, outputs, targets, text_embeddings=None):
        """Performs the matching

        Params:
            outputs: This is a dict that contains at least these entries:
                 "pred_logits": Tensor of dim [batch_size, num_queries, num_classes] with the classification logits
                 "pred_boxes": Tensor of dim [batch_size, num_queries, 4] with the predicted box coordinates
                 "pred_embed": Tensor of dim [batch_size, num_queries, embed_dim] with visual embeddings (optional)

            targets: This is a list of targets (len(targets) = batch_size), where each target is a dict containing:
                 "labels": Tensor of dim [num_target_boxes] containing the class labels
                 "boxes": Tensor of dim [num_target_boxes, 4] containing the target box coordinates

            text_embeddings: Text embeddings from CLIP for cross-modal alignment (optional).

        Returns:
            A list of size batch_size, containing tuples of (index_i, index_j) where:
                - index_i is the indices of the selected predictions (in order)
                - index_j is the indices of the corresponding selected targets (in order)
            For each batch element, it holds:
                len(index_i) = len(index_j) = min(num_queries, num_target_boxes)
        """
        with torch.no_grad():
            bs, num_queries = outputs["pred_logits"].shape[:2]

            # We flatten to compute the cost matrices in a batch
            out_prob = outputs["pred_logits"].flatten(0, 1).sigmoid()
            out_bbox = outputs["pred_boxes"].flatten(0, 1)  # [batch_size * num_queries, 4]

            # Also concat the target labels and boxes
            tgt_ids = torch.cat([v["labels"] for v in targets])
            tgt_bbox = torch.cat([v["boxes"] for v in targets])

            # Compute the classification cost.
            alpha = 0.25
            gamma = 2.0
            neg_cost_class = (1 - alpha) * (out_prob ** gamma) * (-(1 - out_prob + 1e-8).log())
            pos_cost_class = alpha * ((1 - out_prob) ** gamma) * (-(out_prob + 1e-8).log())
            cost_class = pos_cost_class[:, tgt_ids] - neg_cost_class[:, tgt_ids]

            # Compute the L1 cost between boxes
            cost_bbox = torch.cdist(out_bbox, tgt_bbox, p=1)

            # Compute the giou cost between boxes
            cost_giou = -generalized_box_iou(
                box_cxcywh_to_xyxy(out_bbox), box_cxcywh_to_xyxy(tgt_bbox)
            )

            # Compute embedding distance cost if embeddings are provided
            if text_embeddings is not None and "pred_embed" in outputs:
                visual_embeddings = outputs["pred_embed"].flatten(0, 1)  # [batch_size * num_queries, embed_dim]
                # L2 normalization for similarity computation
                visual_embeddings = nn.functional.normalize(visual_embeddings, dim=1)
                text_embeddings = text_embeddings[tgt_ids]  # Get text embeddings for corresponding target labels
                # Compute cosine distance as the embedding cost
                cost_embed = 1 - torch.matmul(visual_embeddings, text_embeddings.t())
            else:
                cost_embed = torch.zeros_like(cost_class)

            # Final cost matrix
            C = (
                self.cost_bbox * cost_bbox
                + self.cost_class * cost_class
                + self.cost_giou * cost_giou
                + self.cost_embed * cost_embed
            )
            C = C.view(bs, num_queries, -1).cpu()

            sizes = [len(v["boxes"]) for v in targets]
            indices = [linear_sum_assignment(c[i]) for i, c in enumerate(C.split(sizes, -1))]
            return [
                (torch.as_tensor(i, dtype=torch.int64), torch.as_tensor(j, dtype=torch.int64))
                for i, j in indices
            ]


class OVHungarianMatcher(HungarianMatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @torch.no_grad()
    def forward(self, outputs, targets, select_id, text_embeddings=None):
        # We flatten to compute the cost matrices in a batch
        num_patch = len(select_id)
        bs, num_queries = outputs["pred_logits"].shape[:2]
        num_queries = num_queries // num_patch
        out_prob_all = outputs["pred_logits"].view(bs, num_patch, num_queries, -1)
        out_bbox_all = outputs["pred_boxes"].view(bs, num_patch, num_queries, -1)

        # Also concat the target labels and boxes
        tgt_ids_all = torch.cat([v["labels"] for v in targets])
        tgt_bbox_all = torch.cat([v["boxes"] for v in targets])

        alpha = 0.25
        gamma = 2.0

        ans = [[[], []] for _ in range(bs)]

        for index, label in enumerate(select_id):
            out_prob = out_prob_all[:, index, :, :].flatten(0, 1).sigmoid()
            out_bbox = out_bbox_all[:, index, :, :].flatten(0, 1)

            mask = (tgt_ids_all == label).nonzero().squeeze(1)
            tgt_bbox = tgt_bbox_all[mask]

            # Compute the classification cost.
            neg_cost_class = (1 - alpha) * (out_prob ** gamma) * (-(1 - out_prob + 1e-8).log())
            pos_cost_class = alpha * ((1 - out_prob) ** gamma) * (-(out_prob + 1e-8).log())
            cost_class = pos_cost_class[:, 0:1] - neg_cost_class[:, 0:1]

            # Compute the L1 cost between boxes
            cost_bbox = torch.cdist(out_bbox, tgt_bbox, p=1)

            # Compute the giou cost between boxes
            cost_giou = -generalized_box_iou(
                box_cxcywh_to_xyxy(out_bbox), box_cxcywh_to_xyxy(tgt_bbox)
            )

            # Compute embedding distance cost if embeddings are provided
            if text_embeddings is not None and "pred_embed" in outputs:
                visual_embeddings = outputs["pred_embed"][:, index, :, :].flatten(0, 1)
                visual_embeddings = nn.functional.normalize(visual_embeddings, dim=1)
                tgt_embeds = text_embeddings[label]  # Get text embeddings for the current label
                cost_embed = 1 - torch.matmul(visual_embeddings, tgt_embeds.t())
            else:
                cost_embed = torch.zeros_like(cost_class)

            # Final cost matrix
            C = (
                self.cost_bbox * cost_bbox
                + self.cost_class * cost_class
                + self.cost_giou * cost_giou
                + self.cost_embed * cost_embed
            )
            C = C.view(bs, num_queries, -1).cpu()

            sizes = [len(v["labels"][v["labels"] == label]) for ind, v in enumerate(targets)]
            indices = [linear_sum_assignment(c[i]) for i, c in enumerate(C.split(sizes, -1))]

            for ind in range(bs):
                x, y = indices[ind]
                if len(x) == 0:
                    continue
                x += index * num_queries
                ans[ind][0] += x.tolist()
                y_label = (targets[ind]["labels"] == label).nonzero().squeeze(1).data.cpu().numpy()
                y_label = y_label[y].tolist()
                ans[ind][1] += y_label

        return [
            (torch.as_tensor(i, dtype=torch.int64), torch.as_tensor(j, dtype=torch.int64))
            for i, j in ans
        ]


def build_matcher(args):
    return OVHungarianMatcher(
        cost_class=args.set_cost_class,
        cost_bbox=args.set_cost_bbox,
        cost_giou=args.set_cost_giou,
        cost_embed=args.feature_loss_coef,  # weight for embedding cost
    ), HungarianMatcher(
        cost_class=args.set_cost_class,
        cost_bbox=args.set_cost_bbox,
        cost_giou=args.set_cost_giou,
    )

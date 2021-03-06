import numpy as np
import torch

from utils_.boxes_utils import bbox_overlaps, bbox_transform, _unmap


# rpn targets
def rpn_targets(all_anchors_boxes, gt_boxes_c, im_info, args):
    """
    Arguments:
        all_anchors_boxes (Tensor) : (H/16 * W/16 * 9, 4)
        gt_boxes_c (Ndarray) : (# gt boxes, 5) [x, y, x`, y`, class]
        im_info (Tuple) : (Height, Width, Channel, Scale)
        args (argparse.Namespace) : global arguments

    Return:
        labels (Ndarray) : (H/16 * W/16 * 9,)
        bbox_targets (Ndarray) : (H/16 * W/16 * 9, 4)
        bbox_inside_weights (Ndarray) : (H/16 * W/16 * 9, 4)
    """


    # it maybe H/16 * W/16 * 9
    num_anchors = all_anchors_boxes.shape[0]

    # im_info : (H, W, C, S)
    height, width = im_info[:2]

    # only keep anchors inside the image
    _allowed_border = 0
    inds_inside = np.where(
        (all_anchors_boxes[:, 0] >= -_allowed_border) &
        (all_anchors_boxes[:, 1] >= -_allowed_border) &
        (all_anchors_boxes[:, 2] < width + _allowed_border) &  # width
        (all_anchors_boxes[:, 3] < height + _allowed_border)  # height
    )[0]


    # keep only inside anchors
    inside_anchors_boxes = all_anchors_boxes[inds_inside, :]
    assert inside_anchors_boxes.shape[0] > 0, '{0}x{1} -> {2}'.format(height, width, num_anchors)


    # label: 1 is positive, 0 is negative, -1 is dont care
    labels = np.empty((len(inds_inside),), dtype=np.float32)
    labels.fill(-1)

    # overlaps between the anchors and the gt boxes
    # overlaps (ex, gt)

    overlaps = bbox_overlaps(inside_anchors_boxes, gt_boxes_c[:,:-1]).cpu().numpy()

    # anchor와 가장많이 겹치는 gtbox의 index(argmax_overlaps)와 그 IOU(max_overlaps)
    argmax_overlaps = overlaps.argmax(axis=1)
    max_overlaps = overlaps[np.arange(len(inds_inside)), argmax_overlaps] # anchor와 가장 많이 겹치는 IOU


    gt_argmax_overlaps = overlaps.argmax(axis=0) # gtbox와 가장많이 겹치는 anchor

    # gtbox와 가장많이 겹치는 anchor의 index(gt_argmax_overlaps)와 그 IOU(gt_max_overlaps)
    gt_max_overlaps = overlaps[gt_argmax_overlaps,
                               np.arange(overlaps.shape[1])]
    gt_argmax_overlaps = np.where(overlaps == gt_max_overlaps)[0]

    # assign bg labels first so that positive labels can clobber them
    labels[max_overlaps < args.neg_threshold] = 0

    # fg label: for each gt, anchor with highest overlap
    labels[gt_argmax_overlaps] = 1

    # fg label: above threshold IOU
    labels[max_overlaps >= args.pos_threshold] = 1


    # subsample positive labels if we have too many
    num_fg = int(0.5 * args.rpn_batch_size)
    fg_inds = np.where(labels == 1)[0]
    if len(fg_inds) > num_fg:
        disable_inds = np.random.choice(
            fg_inds, size=(len(fg_inds) - num_fg), replace=False)
        labels[disable_inds] = -1

    # subsample negative labels if we have too many
    num_bg = args.rpn_batch_size - np.sum(labels == 1)
    bg_inds = np.where(labels == 0)[0]
    if len(bg_inds) > num_bg:
        disable_inds = np.random.choice(
            bg_inds, size=(len(bg_inds) - num_bg), replace=False)
        labels[disable_inds] = -1


    # transform boxes to deltas boxes
    bbox_targets = bbox_transform(inside_anchors_boxes, gt_boxes_c[argmax_overlaps, :-1])

    # loss 계산시 positive box mask를 위한 배열
    bbox_inside_weights = np.zeros((bbox_targets.shape[0], 4), dtype=np.float32)

    mask = np.where(labels == 1)[0]
    bbox_inside_weights[mask, :] = [1.0, 1.0, 1.0, 1.0]

    #print(bbox_targets.shape, bbox_inside_weights.shape, labels.shape)
    # map up to original set of anchors
    # inds_inside 는 data로 채우고 나머지는 fill의 값으로 채움. 즉 backround인 box의 target을 채워준다.
    labels = _unmap(labels, num_anchors, inds_inside, fill=-1)
    bbox_targets = _unmap(bbox_targets, num_anchors, inds_inside, fill=0)
    bbox_inside_weights = _unmap(bbox_inside_weights, num_anchors, inds_inside, fill=0)


    return labels, bbox_targets, bbox_inside_weights

def _jitter_gt_boxes(gt_boxes, jitter=0.05):
    """ jitter the gtboxes, before adding them into rois, to be more robust for cls and rgs
    gt_boxes: (G, 4) [x1 ,y1 ,x2, y2] int
    """
    jittered_boxes = gt_boxes.copy()
    ws = jittered_boxes[:, 2] - jittered_boxes[:, 0] + 1.0
    hs = jittered_boxes[:, 3] - jittered_boxes[:, 1] + 1.0
    width_offset = (np.random.rand(jittered_boxes.shape[0]) - 0.5) * jitter * ws
    height_offset = (np.random.rand(jittered_boxes.shape[0]) - 0.5) * jitter * hs
    jittered_boxes[:, 0] += width_offset
    jittered_boxes[:, 2] += width_offset
    jittered_boxes[:, 1] += height_offset
    jittered_boxes[:, 3] += height_offset

    jittered_boxes[jittered_boxes < 0] = 0

    return jittered_boxes


# faster-RCNN targets
def frcnn_targets(prop_boxes, gt_boxes_c, args):
    """
    Arguments:
        prop_boxes (Tensor) : (# proposal boxes , 4)
        gt_boxes_c (Ndarray) : (# gt boxes, 5) [x, y, x`, y`, class]
        test (Bool) : True or False
        args (argparse.Namespace) : global arguments

    Return:
        labels (Ndarray) : (256,)
        roi_boxes_c[:, :-1] : (256, 4)
        targets (Ndarray) : (256, 21 * 4)
        bbox_inside_weights (Ndarray) : (256, 21 * 4)
    """

    gt_labels = gt_boxes_c[:, -1]
    gt_boxes = gt_boxes_c[:, :-1]
    jitter_gt_boxes = _jitter_gt_boxes(gt_boxes)

    all_boxes = np.vstack((prop_boxes, jitter_gt_boxes, gt_boxes))
    zeros = np.zeros((all_boxes.shape[0], 1), dtype=all_boxes.dtype)
    all_boxes_c = np.hstack((all_boxes, zeros))


    num_images = 1

    # number of roi_boxes_c each per image
    rois_per_image = int(args.frcnn_batch_size / num_images)
    #rois_per_image = int(all_boxes_c.shape[0] / num_images) if test == False else int(prop_boxes.shape[0] / num_images)

    # number of foreground roi_boxes_c per image
    fg_rois_per_image = int(np.round(rois_per_image * args.fg_fraction))

    # sample_rois

    # compute each overlaps with each ground truth
    overlaps = bbox_overlaps(
        np.ascontiguousarray(all_boxes_c[:, :-1], dtype=np.float),
        np.ascontiguousarray(gt_boxes, dtype=np.float))

    # overlaps (iou, index of class)
    overlaps = overlaps.cpu().numpy()


    # groundtruth 중에 가장 많이 오버랩되는 아이를 찾음
    # col방향으로 argmax, max
    gt_assignment = overlaps.argmax(axis=1)  # 가장 iou가 높은 class index
    max_overlaps = overlaps.max(axis=1)  # iou가 가장 높은것
    labels = gt_labels[gt_assignment]


    # foreground에 해당하는 box를 찾고 이를 random 하게 섞어줌.
    fg_indices = np.where(max_overlaps >= args.fg_threshold)[0]
    fg_rois_per_this_image = min(fg_rois_per_image, len(fg_indices))

    if len(fg_indices) > 0:

        fg_indices = np.random.choice(fg_indices, size=fg_rois_per_this_image)


    # background에 해당하는 box를 찾고 이를 random 하게 섞어준다.

    bg_indices = np.where((max_overlaps < args.bg_threshold[1]) &
                          (max_overlaps >= 0))[0]


    bg_rois_per_this_image = rois_per_image - fg_rois_per_this_image
    bg_rois_per_this_image = min(bg_rois_per_this_image, len(bg_indices))

    if len(bg_indices) > 0:
        bg_indices = np.random.choice(bg_indices, size=bg_rois_per_this_image)


    keep_inds = np.append(fg_indices, bg_indices)

    labels = labels[keep_inds]
    roi_boxes_c = all_boxes_c[keep_inds]

    # background에 해당하는 label을 0으로 만들어준다
    labels[fg_rois_per_this_image:] = 0


    # _compute_target
    # all_boxes_c 를 delta_boxes 로 바꿔준다.

    # a = np.array([[j] for j in range(10)])
    # a[[0,0,0]]  : [[0],[0],[0]]
    # index array라서 해당 index에 해당하는 array의 객체가 반복되어 연산된다.
    #a = gt_boxes[gt_assignment[keep_inds], :]
    delta_boxes = bbox_transform(roi_boxes_c[:, :-1], gt_boxes[gt_assignment[keep_inds], :])

    # nomalization by precompute mean, std
    # regression branch에 더 강한.. signal을 주는 역할을 하게 되는건가?
    if args.target_normalization:
        delta_boxes = ((delta_boxes - np.array((0.0, 0.0, 0.0, 0.0)))
                   / np.array((0.1, 0.1, 0.2, 0.2)))

    # _get_bbox_regression_labels
    # delta_boxes 을 84 차원의 target으로 만들어준다. faster_rcnn regressor의 아웃풋이 84
    targets = np.zeros((len(labels), 4 * 21), dtype=np.float32)

    # loss 계산시 foreground mask를 위한 array
    bbox_inside_weights = np.zeros(targets.shape, dtype=np.float32)

    # foreground object index
    indices = np.where(labels > 0)[0]

    for index in indices:
        cls = int(labels[index])
        start = 4 * cls
        end = start + 4
        targets[index, start:end] = delta_boxes[index, :]
        bbox_inside_weights[index, start:end] = [1, 1, 1, 1]


    return labels, roi_boxes_c[:, :-1], targets , bbox_inside_weights



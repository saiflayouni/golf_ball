# --------------------------------------------------------
# Pytorch Multi-GPU Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import sys
import numpy as np
import argparse
import pprint
import pdb
import time

import cv2

import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import pickle
from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
# from model.nms.nms_wrapper import nms
from model.roi_layers import nms
from model.rpn.bbox_transform import bbox_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections
from model.faster_rcnn.vgg16 import vgg16
from model.faster_rcnn.resnet import resnet

from PIL import Image
import matplotlib.pyplot as plt
from pykalman import KalmanFilter

try:
    xrange          # Python 2
except NameError:
    xrange = range  # Python 3


def parse_args():
    """
    Parse input arguments
    """
    parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
    parser.add_argument('--dataset', dest='dataset',
                        help='training dataset',
                        default='pascal_voc', type=str)
    parser.add_argument('--cfg', dest='cfg_file',
                        help='optional config file',
                        default='cfgs/vgg16.yml', type=str)
    parser.add_argument('--net', dest='net',
                        help='vgg16, res50, res101, res152',
                        default='res101', type=str)
    parser.add_argument('--set', dest='set_cfgs',
                        help='set config keys', default=None,
                        nargs=argparse.REMAINDER)
    parser.add_argument('--load_dir', dest='load_dir',
                        help='directory to load models', default="models",
                        type=str)
    parser.add_argument('--cuda', dest='cuda',
                        help='whether use CUDA',
                        action='store_true')
    parser.add_argument('--ls', dest='large_scale',
                        help='whether use large imag scale',
                        action='store_true')
    parser.add_argument('--mGPUs', dest='mGPUs',
                        help='whether use multiple GPUs',
                        action='store_true')
    parser.add_argument('--cag', dest='class_agnostic',
                        help='whether perform class_agnostic bbox regression',
                        action='store_true')
    parser.add_argument('--parallel_type', dest='parallel_type',
                        help='which part of model to parallel, 0: all, 1: model before roi pooling',
                        default=0, type=int)
    parser.add_argument('--checksession', dest='checksession',
                        help='checksession to load model',
                        default=1, type=int)
    parser.add_argument('--checkepoch', dest='checkepoch',
                        help='checkepoch to load network',
                        default=1, type=int)
    parser.add_argument('--checkpoint', dest='checkpoint',
                        help='checkpoint to load network',
                        default=10021, type=int)
    parser.add_argument('--vis', dest='vis',
                        help='visualization mode',
                        action='store_true')
    args = parser.parse_args()
    return args


lr = cfg.TRAIN.LEARNING_RATE
momentum = cfg.TRAIN.MOMENTUM
weight_decay = cfg.TRAIN.WEIGHT_DECAY


if __name__ == '__main__':

    args = parse_args()

    print('Called with args:')
    print(args)

    if torch.cuda.is_available() and not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

    np.random.seed(cfg.RNG_SEED)
    if args.dataset == "pascal_voc":
        args.imdb_name = "voc_2007_trainval"
        args.imdbval_name = "voc_2007_test"
        args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
    elif args.dataset == "pascal_voc_0712":
        args.imdb_name = "voc_2007_trainval+voc_2012_trainval"
        args.imdbval_name = "voc_2007_test"
        args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
    elif args.dataset == "coco":
        args.imdb_name = "coco_2014_train+coco_2014_valminusminival"
        args.imdbval_name = "coco_2014_minival"
        args.set_cfgs = ['ANCHOR_SCALES', '[4, 8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
    elif args.dataset == "imagenet":
        args.imdb_name = "imagenet_train"
        args.imdbval_name = "imagenet_val"
        args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
    elif args.dataset == "vg":
        args.imdb_name = "vg_150-50-50_minitrain"
        args.imdbval_name = "vg_150-50-50_minival"
        args.set_cfgs = ['ANCHOR_SCALES', '[4, 8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']

    args.cfg_file = "cfgs/{}_ls.yml".format(args.net) if args.large_scale else "cfgs/{}.yml".format(args.net)

    if args.cfg_file is not None:
        cfg_from_file(args.cfg_file)
    if args.set_cfgs is not None:
        cfg_from_list(args.set_cfgs)

    print('Using config:')
    pprint.pprint(cfg)

    cfg.TRAIN.USE_FLIPPED = False
    imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name, False)
    imdb.competition_mode(on=True)

    print('{:d} roidb entries'.format(len(roidb)))

    input_dir = args.load_dir + "/" + args.net + "/" + args.dataset
    if not os.path.exists(input_dir):
        raise Exception('There is no input directory for loading network from ' + input_dir)
    load_name = os.path.join(input_dir,
                             'faster_rcnn_{}_{}_{}.pth'.format(args.checksession, args.checkepoch, args.checkpoint))

    # initilize the network here.
    if args.net == 'vgg16':
        fasterRCNN = vgg16(imdb.classes, pretrained=False, class_agnostic=args.class_agnostic)
    elif args.net == 'res101':
        fasterRCNN = resnet(imdb.classes, 101, pretrained=False, class_agnostic=args.class_agnostic)
    elif args.net == 'res50':
        fasterRCNN = resnet(imdb.classes, 50, pretrained=False, class_agnostic=args.class_agnostic)
    elif args.net == 'res152':
        fasterRCNN = resnet(imdb.classes, 152, pretrained=False, class_agnostic=args.class_agnostic)
    else:
        print("network is not defined")
        pdb.set_trace()

    fasterRCNN.create_architecture()

    print("load checkpoint %s" % (load_name))
    checkpoint = torch.load(load_name)
    fasterRCNN.load_state_dict(checkpoint['model'])
    if 'pooling_mode' in checkpoint.keys():
        cfg.POOLING_MODE = checkpoint['pooling_mode']

    print('load model successfully!')
    # initilize the tensor holder here.
    im_data = torch.FloatTensor(1)
    im_info = torch.FloatTensor(1)
    num_boxes = torch.LongTensor(1)
    gt_boxes = torch.FloatTensor(1)

    # ship to cuda
    if args.cuda:
        im_data = im_data.cuda()
        im_info = im_info.cuda()
        num_boxes = num_boxes.cuda()
        gt_boxes = gt_boxes.cuda()

    # make variable
    im_data = Variable(im_data)
    im_info = Variable(im_info)
    num_boxes = Variable(num_boxes)
    gt_boxes = Variable(gt_boxes)

    if args.cuda:
        cfg.CUDA = True

    if args.cuda:
        fasterRCNN.cuda()

    start = time.time()
    max_per_image = 1

    vis = args.vis

    if vis:
        thresh = 0.05
    else:
        thresh = 0.0

    save_name = 'faster_rcnn_10'
    num_images = len(imdb.image_index)
    all_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(imdb.num_classes)]

    output_dir = get_output_dir(imdb, save_name)
    dataset = roibatchLoader(roidb, ratio_list, ratio_index, 1,
                             imdb.num_classes, training=False, normalize=False)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=1,
                                             shuffle=False, num_workers=0,
                                             pin_memory=True)

    data_iter = iter(dataloader)

    _t = {'im_detect': time.time(), 'misc': time.time()}
    det_file = os.path.join(output_dir, 'detections.pkl')

    fasterRCNN.eval()
    empty_array = np.transpose(np.array([[], [], [], [], []]), (1, 0))

    """
    Kalman Filter
    """

    Transition_Matrix = [[1, 0, 1, 0], [0, 1, 0, 1], [0, 0, 1, 0], [0, 0, 0, 1]]
    Observation_Matrix = [[1, 0, 1, 0], [0, 1, 0, 1]]

    np.set_printoptions(suppress=True)
    xhat = np.zeros((num_images + 1, 4))
    P = np.zeros((num_images + 1, 4, 4))
    xhatminus = np.zeros((num_images + 1, 4))
    Pminus = np.zeros((num_images + 1, 4, 4))
    K = np.zeros((num_images + 1, 4, 2))
    Q = 0.1 * np.eye(4)
    R = 0.0001 * np.eye(2)
    measurements = []

    # initCovariance = 0.0001 * np.eye(4)
    # transitionCovariance = 0.1 * np.eye(4)
    # observationCovariance = 0.0001 * np.eye(2)
    # filtered_state_means = np.zeros((num_images+1, 4))
    # filtered_state_covariances = np.zeros((num_images+1, 4, 4))

    # kalman = KalmanFilter(transition_matrices=Transition_Matrix,
    #                      observation_matrices=Observation_Matrix,
    #                      initial_state_mean=np.zeros(4),
    #                      initial_state_covariance=initCovariance,
    #                      transition_covariance=transitionCovariance,
    #                      observation_covariance=observationCovariance)

    """
    Kalman Filter END
    """
    average_detection_time = 0
    with open('./data/VOCdevkit2007/VOC2007/ImageSets/Main/test.txt') as f:
        image_index = [x.strip() for x in f.readlines()]

    f_16 = open('Put_Tracking/Put_5_tracking.txt', 'w')
    for i in range(num_images):

        directory = './data/VOCdevkit2007/VOC2007/JPEGImages/' + image_index[i] + '.jpg'
        image = Image.open(directory)
        image = np.array(image)

        det_tic = time.time()

        xhatminus[i + 1] = np.matmul(Transition_Matrix, xhat[i])
        Pminus[i + 1] = np.matmul(np.matmul(Transition_Matrix, P[i]), Transition_Matrix) + Q
        # xhatminus = np.matmul(Transition_Matrix, filtered_state_means[i])
        if i == 0:

            # 1
            # xmin = 994
            # ymin = 991
            # xmax = 1011
            # ymax = 1011

            # 2
            # xmin = 1152
            # ymin = 1027
            # xmax = 1179
            # ymax = 1052

            # 4
            # xmin = 1015
            # ymin = 589
            # xmax = 1028
            # ymax = 600

            # 5
            # xmin = 946
            # ymin = 963
            # xmax = 966
            # ymax = 983

            # 8
            # xmin = 1169
            # ymin = 941
            # xmax = 1191
            # ymax = 963

            # 10
            # xmin = 1117
            # ymin = 992
            # xmax = 1143
            # ymax = 1018

            # 16
            # xmin = 895
            # ymin = 962
            # xmax = 915
            # ymax = 982

            # 17
            # xmin = 1181
            # ymin = 985
            # xmax = 1206
            # ymax = 1009

            # Put_1
            # xmin = 1380
            # ymin = 803
            # xmax = 1395
            # ymax = 817

            # Put_2
            # xmin = 500
            # ymin = 902
            # xmax = 520
            # ymax = 920

            # Put_3
            # xmin = 518
            # ymin = 692
            # xmax = 534
            # ymax = 707

            # Put_4
            # xmin = 1206
            # ymin = 767
            # xmax = 1222
            # ymax = 783

            # Put_5
            xmin = 1397
            ymin = 826
            xmax = 1420
            ymax = 845

            x_center = int((xmin + xmax) / 2)
            y_center = int((ymin + ymax) / 2)
        else:
            # x_center = xhatminus[0]
            # y_center = xhatminus[1]
            x_center = xhatminus[i + 1, 0]
            y_center = xhatminus[i + 1, 1]

        left = int(round(max(x_center - 150, 0 + 1)))
        upper = int(round(max(y_center - 150, 0 + 1)))
        right = int(round(min(x_center + 150, 1920 - 1)))
        lower = int(round(min(y_center + 150, 1080 - 1)))

        # directory = './data/VOCdevkit2007/VOC2007/JPEGImages/' + image_index[i] + '.jpg'
        # image = Image.open(directory)

        # image_crop = image.crop((left, upper, right, lower))
        # print(image.shape)
        image_crop = image[upper:lower, left:right]
        detect_time = time.time() - det_tic

        image_crop = Image.fromarray(image_crop)
        image_crop.save(directory)

        data = next(data_iter)
        with torch.no_grad():
            im_data.resize_(data[0].size()).copy_(data[0])
            im_info.resize_(data[1].size()).copy_(data[1])
            gt_boxes.resize_(data[2].size()).copy_(data[2])
            num_boxes.resize_(data[3].size()).copy_(data[3])
        # print(im_data.size())

        det_tic = time.time()
        rois, cls_prob, bbox_pred, \
            rpn_loss_cls, rpn_loss_box, \
            RCNN_loss_cls, RCNN_loss_bbox, \
            rois_label = fasterRCNN(im_data, im_info, gt_boxes, num_boxes)

        scores = cls_prob.data
        boxes = rois.data[:, :, 1:5]

        if cfg.TEST.BBOX_REG:
            # Apply bounding-box regression deltas
            box_deltas = bbox_pred.data
            if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
                # Optionally normalize targets by a precomputed mean and stdev
                if args.class_agnostic:
                    box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                        + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
                    box_deltas = box_deltas.view(1, -1, 4)
                else:
                    box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                        + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
                    box_deltas = box_deltas.view(1, -1, 4 * len(imdb.classes))

            pred_boxes = bbox_transform_inv(boxes, box_deltas, 1)
            pred_boxes = clip_boxes(pred_boxes, im_info.data, 1)
        else:
            # Simply repeat the boxes, once for each class
            pred_boxes = np.tile(boxes, (1, scores.shape[1]))

        pred_boxes /= data[1][0][2].item()

        scores = scores.squeeze()
        pred_boxes = pred_boxes.squeeze()
        # det_toc = time.time()
        # detect_time = det_toc - det_tic
        misc_tic = time.time()
        if vis:
            im = cv2.imread(imdb.image_path_at(i))
            im2show = np.copy(im)
        for j in xrange(1, imdb.num_classes):
            inds = torch.nonzero(scores[:, j] > thresh).view(-1)
            # if there is det
            if inds.numel() > 0:
                cls_scores = scores[:, j][inds]
                _, order = torch.sort(cls_scores, 0, True)
                if args.class_agnostic:
                    cls_boxes = pred_boxes[inds, :]
                else:
                    cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4]

                cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1)
                # cls_dets = torch.cat((cls_boxes, cls_scores), 1)
                cls_dets = cls_dets[order]
                keep = nms(cls_boxes[order, :], cls_scores[order], cfg.TEST.NMS)
                cls_dets = cls_dets[keep.view(-1).long()]
                if vis:
                    im2show = vis_detections(im2show, imdb.classes[j], cls_dets.cpu().numpy(), 0.3)
                all_boxes[j][i] = cls_dets.cpu().numpy()
            else:
                all_boxes[j][i] = empty_array

        # Limit to max_per_image detections *over all classes*
        if max_per_image > 0:
            image_scores = np.hstack([all_boxes[j][i][:, -1]
                                      for j in xrange(1, imdb.num_classes)])
            if len(image_scores) > max_per_image:
                image_thresh = np.sort(image_scores)[-max_per_image]
                for j in xrange(1, imdb.num_classes):
                    keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                    all_boxes[j][i] = all_boxes[j][i][keep, :]

        # Kalman Filter

        dets = all_boxes[j][i]
        dets[:, 0] = dets[:, 0] + left
        dets[:, 1] = dets[:, 1] + upper
        dets[:, 2] = dets[:, 2] + left
        dets[:, 3] = dets[:, 3] + upper
        all_boxes[j][i] = dets

        dets = all_boxes[j][i]
        xmin = dets[0, 0] + 1
        ymin = dets[0, 1] + 1
        xmax = dets[0, 2] + 1
        ymax = dets[0, 3] + 1

        x_center = int((xmin + xmax) / 2)
        y_center = int((ymin + ymax) / 2)
        x_width = xmax - xmin
        y_height = ymax - ymin
        xy = str(int(round(xmin))) + ' ' + str(int(round(ymin))) + ' ' + str(int(round(x_width))) + ' ' + str(int(round(y_height))) + '\n'
        f_16.write(xy)
        measurement = [x_center, y_center]
        # measurements.append(measurement)

        # filtered_state_means[i+1], filtered_state_covariances[i+1] = kalman.filter_update(filtered_state_means[i], filtered_state_covariances[i], observation=measurement, observation_matrix=np.asarray(Observation_Matrix))

        K[i + 1] = np.matmul(np.matmul(Pminus[i + 1], np.transpose(Observation_Matrix)),
                             np.linalg.inv(np.matmul(np.matmul(Observation_Matrix, Pminus[i + 1]),
                                                     np.transpose(Observation_Matrix)) + R))
        xhat[i + 1] = xhatminus[i + 1] + np.matmul(K[i + 1], (measurement - np.matmul(Observation_Matrix, xhatminus[i + 1])))
        P[i + 1] = np.matmul(np.eye(4) - np.matmul(K[i + 1], Observation_Matrix), Pminus[i + 1])

        # det_toc = time.time()
        detect_time += time.time() - det_tic
        average_detection_time += detect_time
        # print(filtered_state_means[i+1], measurement)

        x_width = xmax - xmin
        y_height = ymax - ymin
        xy = str(int(round(xmin))) + ' ' + str(int(round(ymin))) + ' ' + str(int(round(x_width))) + ' ' + str(int(round(y_height))) + '\n'
        f_16.write(xy)

        # Kalman Filter END

        misc_toc = time.time()
        nms_time = misc_toc - misc_tic

        sys.stdout.write('im_detect: {:d}/{:d} {:.3f}s {:.3f}s   \r'
                         .format(i + 1, num_images, detect_time, nms_time))
        sys.stdout.flush()

        if vis:
            cv2.imwrite('result.png', im2show)
            pdb.set_trace()
            # cv2.imshow('test', im2show)
            # cv2.waitKey(0)

    with open(det_file, 'wb') as f:
        pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL)

    print('Evaluating detections')
    imdb.evaluate_detections(all_boxes, output_dir)

    end = time.time()
    print("test time: %0.4fs" % (end - start))

    average_detection_time /= num_images
    print("average detection time: ", average_detection_time)

    f_16.close()
    os.remove('./data/cache/voc_2007_test_gt_roidb.pkl')
    os.rmdir('./data/cache')
    os.rmdir('./data/VOCdevkit2007/annotations_cache')
    os.remove('./data/VOCdevkit2007/VOC2007/ImageSets/Main/test.txt_annots.pkl')
#!/usr/bin/env python3
from __future__ import division

import argparse
import os
import sys

root_folder = os.path.abspath(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.append(root_folder)  # to enable import from parent directory

import numpy as np
import torch
import tqdm
from terminaltables import AsciiTable
from torch.autograd import Variable

# from tools import wandb_logger
from tools.dataset import HITUAVDatasetTest
from model import load_model
from parse_config import parse_data_config
from utils import ap_per_class
from utils import get_batch_statistics
from utils import load_classes
from utils import non_max_suppression
from utils import print_environment_info
from utils import xywh2xyxy


def evaluate_model_file(
    model_path,
    weights_path,
    img_path,
    class_names,
    batch_size=8,
    img_size=416,
    n_cpu=8,
    iou_thres=0.5,
    conf_thres=0.5,
    nms_thres=0.5,
    verbose=True,
):
    """Evaluate model on validation dataset.
    :param model_path: Path to model definition file (.cfg)
    :type model_path: str
    :param weights_path: Path to weights or checkpoint file (.weights or .pth)
    :type weights_path: str
    :param img_path: Path to file containing all paths to validation images.
    :type img_path: str
    :param class_names: List of class names
    :type class_names: [str]
    :param batch_size: Size of each image batch, defaults to 8
    :type batch_size: int, optional
    :param img_size: Size of each image dimension for yolo, defaults to 416
    :type img_size: int, optional
    :param n_cpu: Number of cpu threads to use during batch generation, defaults to 8
    :type n_cpu: int, optional
    :param iou_thres: IOU threshold required to qualify as detected, defaults to 0.5
    :type iou_thres: float, optional
    :param conf_thres: Object confidence threshold, defaults to 0.5
    :type conf_thres: float, optional
    :param nms_thres: IOU threshold for non-maximum suppression, defaults to 0.5
    :type nms_thres: float, optional
    :param verbose: If True, prints stats of model, defaults to True
    :type verbose: bool, optional
    :return: Returns precision, recall, AP, f1, ap_class
    """
    workers = 4  # number of workers for loading data in the DataLoader
    val_dataset = HITUAVDatasetTest(img_path, yolo=True)
    dataloader = torch.utils.data.DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=val_dataset.yolo_collate_fn,
        num_workers=workers,
        pin_memory=True,
    )  # note that we're passing the collate function here
    model = load_model(model_path, weights_path)
    metrics_output = _evaluate(
        model,
        dataloader,
        class_names,
        img_size,
        iou_thres,
        conf_thres,
        nms_thres,
        verbose,
    )
    return metrics_output


def print_eval_stats(metrics_output, class_names, verbose):
    if metrics_output is not None:
        precision, recall, AP, f1, ap_class = metrics_output
        if verbose:
            # Prints class AP and mean AP
            ap_table = [["Index", "Class", "AP"]]
            for i, c in enumerate(ap_class):
                ap_table += [[c, class_names[i], "%.5f" % AP[i]]]
            print(AsciiTable(ap_table).table)
        print(f"---- mAP {AP.mean():.5f} ----")
    else:
        print("---- mAP not measured (no detections found by model) ----")


def _evaluate(
    model,
    dataloader,
    class_names,
    img_size,
    iou_thres,
    conf_thres,
    nms_thres,
    verbose,
    epoch,
):
    """Evaluate model on validation dataset.
    :param model: Model to evaluate
    :type model: models.Darknet
    :param dataloader: Dataloader provides the batches of images with targets
    :type dataloader: DataLoader
    :param class_names: List of class names
    :type class_names: [str]
    :param img_size: Size of each image dimension for yolo
    :type img_size: int
    :param iou_thres: IOU threshold required to qualify as detected
    :type iou_thres: float
    :param conf_thres: Object confidence threshold
    :type conf_thres: float
    :param nms_thres: IOU threshold for non-maximum suppression
    :type nms_thres: float
    :param verbose: If True, prints stats of model
    :type verbose: bool
    :return: Returns precision, recall, AP, f1, ap_class
    """
    model.eval()  # Set model to evaluation mode

    Tensor = torch.cuda.FloatTensor if torch.cuda.is_available() else torch.FloatTensor

    labels = []
    sample_metrics = []  # List of tuples (TP, confs, pred)
    image_list = []  # for wandb slider visualization
    for batch_i, (imgs, targets) in enumerate(tqdm.tqdm(dataloader, desc="Validating")):
        # Extract labels
        labels += targets[:, 1].tolist()
        # Rescale target
        targets[:, 2:] = xywh2xyxy(targets[:, 2:])
        targets[:, 2:] *= img_size

        imgs = Variable(imgs.type(Tensor), requires_grad=False)

        with torch.no_grad():
            outputs = model(imgs)
            outputs = non_max_suppression(
                outputs, conf_thres=conf_thres, iou_thres=nms_thres
            )

        sample_metrics += get_batch_statistics(
            outputs, targets, iou_threshold=iou_thres
        )

        # wandb_logger.add_batch(
        #     images=imgs,
        #     predictions=outputs,
        #     ground_truths=[
        #         targets[targets[:, 0] == image_index][:, 1:]
        #         for image_index in range(int(targets[0, 0]), int(targets[-1, 0] + 1))
        #     ],  # change to list of tensor each contain boxes of its image
        #     class_id_to_label={id: name for id, name in enumerate(class_names)},
        #     image_list=image_list,
        #     box_unit="pixel",
        # )  # add batch to image list before bulk upload

    # wandb_logger.log({"eval/images": image_list, "epoch": epoch})

    if len(sample_metrics) == 0:  # No detections over whole validation set.
        print("---- No detections over whole validation set ----")
        return None

    # Concatenate sample statistics
    true_positives, pred_scores, pred_labels = [
        np.concatenate(x, 0) for x in list(zip(*sample_metrics))
    ]
    metrics_output = ap_per_class(true_positives, pred_scores, pred_labels, labels)

    print_eval_stats(metrics_output, class_names, verbose)

    return metrics_output


def _create_validation_data_loader(data_folder="./", batch_size=8, workers=4):
    """
    Creates a DataLoader for validation.
    :param img_path: Path to file containing all paths to validation images.
    :type img_path: str
    :param batch_size: Size of each image batch
    :type batch_size: int
    :param img_size: Size of each image dimension for yolo
    :type img_size: int
    :param n_cpu: Number of cpu threads to use during batch generation
    :type n_cpu: int
    :return: Returns DataLoader
    :rtype: DataLoader
    """
    test_dataset = HITUAVDatasetTest(data_folder)
    dataloader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=test_dataset.collate_fn,
        num_workers=workers,
        pin_memory=True,
    )  # note that we're passing the collate function here
    return dataloader


def run():
    print_environment_info()
    parser = argparse.ArgumentParser(description="Evaluate validation data.")
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        default="./model/yolo/yolov3-custom.cfg",
        help="Path to model definition file (.cfg)",
    )
    parser.add_argument(
        "-w",
        "--weights",
        type=str,
        default="yolo_logs/2022_11_01__01_56_27/yolov3_ckpt_900.pth",
        help="Path to weights or checkpoint file (.weights or .pth)",
    )
    parser.add_argument(
        "-d",
        "--data",
        type=str,
        default="hit.data",
        help="Path to data config file (.data)",
    )
    parser.add_argument(
        "-b", "--batch_size", type=int, default=8, help="Size of each image batch"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Makes the validation more verbose"
    )
    parser.add_argument(
        "--img_size",
        type=int,
        default=416,
        help="Size of each image dimension for yolo",
    )
    parser.add_argument(
        "--n_cpu",
        type=int,
        default=8,
        help="Number of cpu threads to use during batch generation",
    )
    parser.add_argument(
        "--iou_thres",
        type=float,
        default=0.5,
        help="IOU threshold required to qualify as detected",
    )
    parser.add_argument(
        "--conf_thres", type=float, default=0.01, help="Object confidence threshold"
    )
    parser.add_argument(
        "--nms_thres",
        type=float,
        default=0.4,
        help="IOU threshold for non-maximum suppression",
    )
    args = parser.parse_args()
    print(f"Command line arguments: {args}")

    # Load configuration from data file
    data_config = parse_data_config("dataset.cfg")
    # Path to file containing all images for validation
    valid_path = "./"
    class_names = load_classes(data_config["names"])  # List of class names

    precision, recall, AP, f1, ap_class = evaluate_model_file(
        args.model,
        args.weights,
        valid_path,
        class_names,
        batch_size=args.batch_size,
        img_size=args.img_size,
        n_cpu=args.n_cpu,
        iou_thres=args.iou_thres,
        conf_thres=args.conf_thres,
        nms_thres=args.nms_thres,
        verbose=True,
    )
    print(AP.mean())


if __name__ == "__main__":
    run()

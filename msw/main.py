import tensorflow as tf
import numpy as np
import sys
import argparse
import itertools
import math
import cv2
from attrdict import AttrDict
from dataset import Dataset
from model import Model
from networks.residual_network import ResidualNetwork
from networks.attention_network import AttentionNetwork

parser = argparse.ArgumentParser()
parser.add_argument("--model_dir", type=str, default="model", help="model directory")
parser.add_argument('--filenames', type=str, nargs="+", default=["train.tfrecord"], help="tfrecord filenames")
parser.add_argument("--num_epochs", type=int, default=10, help="number of training epochs")
parser.add_argument("--batch_size", type=int, default=128, help="batch size")
parser.add_argument("--buffer_size", type=int, default=900000, help="buffer size to shuffle dataset")
parser.add_argument("--train", action="store_true", help="with training")
parser.add_argument("--eval", action="store_true", help="with evaluation")
parser.add_argument("--predict", action="store_true", help="with prediction")
parser.add_argument("--gpu", type=str, default="0,1,2", help="gpu id")
args = parser.parse_args()

tf.logging.set_verbosity(tf.logging.INFO)

sys.setrecursionlimit(10000)


def scale(input, input_min, input_max, output_min, output_max):
    return output_min + (input - input_min) / (input_max - input_min) * (output_max - output_min)


def search_bounding_box(image, threshold):

    if len(image.shape) == 3 and image.shape[-1] == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    max_value = 1.0 if np.issubdtype(image.dtype, np.floating) else 255
    binary = cv2.threshold(image, threshold, max_value, cv2.THRESH_BINARY)[1]
    flags = np.ones_like(binary, dtype=np.bool)
    h, w = binary.shape[:2]
    segments = []

    def depth_first_search(y, x):

        segments[-1].append((y, x))
        flags[y][x] = False

        for dy, dx in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
            if 0 <= y + dy < h and 0 <= x + dx < w:
                if flags[y + dy, x + dx] and binary[y + dy, x + dx]:
                    depth_first_search(y + dy, x + dx)

    for y in range(flags.shape[0]):
        for x in range(flags.shape[1]):
            if flags[y, x] and binary[y, x]:
                segments.append([])
                depth_first_search(y, x)

    bounding_boxes = [(lambda ls_1, ls_2: ((min(ls_1), min(ls_2)), (max(ls_1), max(ls_2))))(*zip(*segment)) for segment in segments]
    bounding_boxes = sorted(bounding_boxes, key=lambda box: abs(box[0][0] - box[1][0]) * abs(box[0][1] - box[1][1]))

    return bounding_boxes[-1]


def main(unused_argv):

    classifier = tf.estimator.Estimator(
        model_fn=Model(
            convolutional_network=ResidualNetwork(
                conv_param=AttrDict(filters=64, kernel_size=[7, 7], strides=[2, 2]),
                pool_param=None,
                residual_params=[
                    AttrDict(filters=64, strides=[2, 2], blocks=2),
                    AttrDict(filters=128, strides=[2, 2], blocks=2),
                ],
                num_classes=None,
                data_format="channels_last"
            ),
            attention_network=AttentionNetwork(
                conv_params=[
                    AttrDict(filters=4, kernel_size=[9, 9], strides=[2, 2]),
                    AttrDict(filters=4, kernel_size=[9, 9], strides=[2, 2]),
                ],
                deconv_params=[
                    AttrDict(filters=16, kernel_size=[3, 3], strides=[2, 2]),
                    AttrDict(filters=16, kernel_size=[3, 3], strides=[2, 2]),
                ],
                rnn_params=[
                    AttrDict(sequence_length=4, num_units=[256]),
                    AttrDict(sequence_length=10, num_units=[256])
                ],
                data_format="channels_last"
            ),
            num_classes=63,
            num_tiles=1,
            data_format="channels_last",
            accuracy_type=Model.AccuracyType.EDIT_DISTANCE,
            hyper_params=AttrDict(
                cross_entropy_decay=1.0,
                attention_map_decay=0.001
            )
        ),
        model_dir=args.model_dir,
        config=tf.estimator.RunConfig().replace(
            session_config=tf.ConfigProto(
                gpu_options=tf.GPUOptions(
                    visible_device_list=args.gpu,
                    allow_growth=True
                )
            )
        )
    )

    if args.train:

        classifier.train(
            input_fn=lambda: Dataset(
                filenames=args.filenames,
                num_epochs=args.num_epochs,
                batch_size=args.batch_size,
                buffer_size=args.buffer_size,
                image_size=None,
                data_format="channels_last",
                sequence_length=4,
                string_length=10
            ).get_next()
        )

    if args.eval:

        eval_results = classifier.evaluate(
            input_fn=lambda: Dataset(
                filenames=args.filenames,
                num_epochs=args.num_epochs,
                batch_size=args.batch_size,
                buffer_size=args.buffer_size,
                image_size=None,
                data_format="channels_last",
                sequence_length=4,
                string_length=10
            ).get_next()
        )

        print(eval_results)

    if args.predict:

        predict_results = classifier.predict(
            input_fn=lambda: Dataset(
                filenames=args.filenames,
                num_epochs=args.num_epochs,
                batch_size=args.batch_size,
                buffer_size=args.buffer_size,
                image_size=None,
                data_format="channels_last",
                sequence_length=4,
                string_length=10
            ).get_next()
        )

        class_ids = {}

        for j in range(ord("0"), ord("z") + 1):

            if ord("0") <= j <= ord("9"):
                class_ids[chr(j)] = j - ord("0")
            elif ord("A") <= j <= ord("Z"):
                class_ids[chr(j)] = j - ord("A") + class_ids["9"] + 1
            elif ord("a") <= j <= ord("z"):
                class_ids[chr(j)] = j - ord("a") + class_ids["Z"] + 1

        class_ids[""] = max(class_ids.values()) + 1

        chars = {class_id: char for char, class_id in class_ids.items()}

        for i, predict_result in enumerate(itertools.islice(predict_results, 1)):

            images = predict_result["images"]
            merged_attention_maps = predict_result["merged_attention_maps"]
            predictions = predict_result["predictions"]

            for j in range(merged_attention_maps.shape[1]):

                prediction = "".join([chars[class_id] for class_id in predictions[j]])

                if not prediction:
                    continue

                attention_map_images = []
                boundin_box_images = []

                for k in range(merged_attention_maps.shape[2]):

                    if not k % (int(math.sqrt(merged_attention_maps.shape[2]))):

                        attention_map_images.append([])
                        boundin_box_images.append([])

                        merged_attention_map = merged_attention_maps[0, j, k]
                        merged_attention_map = scale(merged_attention_map, merged_attention_map.min(), merged_attention_map.max(), 0.0, 1.0)
                        merged_attention_map = cv2.resize(merged_attention_map, (256, 256))
                        bounding_box = search_bounding_box(merged_attention_map, 0.5)

                        attention_map_image = np.copy(images[0])
                        attention_map_image += np.pad(np.expand_dims(merged_attention_map, axis=-1), [[0, 0], [0, 0], [0, 2]], "constant")
                        attention_map_images[-1].append(attention_map_image)

                        boundin_box_image = np.copy(images[0])
                        boundin_box_image = cv2.rectangle(boundin_box_image, bounding_box[0][::-1], bounding_box[1][::-1], (255, 0, 0), 2)
                        boundin_box_images[-1].append(boundin_box_image)

                else:

                    while len(attention_map_images[0]) != len(attention_map_images[-1]):
                        print("a")
                        attention_map_images[-1].append(np.zeros_like(attention_map_image))

                    while len(boundin_box_images[0]) != len(boundin_box_images[-1]):
                        boundin_box_images[-1].append(np.zeros_like(boundin_box_image))

                attention_map_images = np.concatenate([
                    np.concatenate(attention_map_images, axis=1)
                    for attention_map_images in attention_map_images
                ], axis=0)

                boundin_box_images = np.concatenate([
                    np.concatenate(boundin_box_images, axis=1)
                    for boundin_box_images in boundin_box_images
                ], axis=0)

                attention_map_images = cv2.cvtColor(attention_map_images, cv2.COLOR_BGR2RGB)
                boundin_box_images = cv2.cvtColor(boundin_box_images, cv2.COLOR_BGR2RGB)

                attention_map_images = scale(attention_map_images, 0.0, 1.0, 0.0, 255.0)
                boundin_box_images = scale(boundin_box_images, 0.0, 1.0, 0.0, 255.0)

                cv2.imwrite("outputs/{}_{}_attention_map.jpg".format(i, prediction), attention_map_images)
                cv2.imwrite("outputs/{}_{}_boundin_box.jpg".format(i, prediction), boundin_box_images)


if __name__ == "__main__":
    tf.app.run()

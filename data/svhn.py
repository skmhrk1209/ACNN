import tensorflow as tf
import numpy as np
from . import dataset


class Dataset(dataset.Dataset):

    def __init__(self, filenames, num_epochs, batch_size, buffer_size, data_format, image_size):

        self.data_format = data_format
        self.image_size = image_size

        super(Dataset, self).__init__(filenames, num_epochs, batch_size, buffer_size)

    def parse(self, example):

        features = tf.parse_single_example(
            serialized=example,
            features={
                "path": tf.FixedLenFeature(
                    shape=[],
                    dtype=tf.string,
                    default_value=""
                ),
                "label": tf.FixedLenFeature(
                    shape=[4],
                    dtype=tf.int64,
                    default_value=[10] * 4
                ),
                "top": tf.FixedLenFeature(
                    shape=[],
                    dtype=tf.int64,
                    default_value=0
                ),
                "left": tf.FixedLenFeature(
                    shape=[],
                    dtype=tf.int64,
                    default_value=0
                ),
                "bottom": tf.FixedLenFeature(
                    shape=[],
                    dtype=tf.int64,
                    default_value=0
                ),
                "right": tf.FixedLenFeature(
                    shape=[],
                    dtype=tf.int64,
                    default_value=0
                ),
            }
        )

        image = tf.read_file(features["path"])
        image = tf.image.decode_png(image, 3)

        top = tf.cast(features["top"], tf.int32)
        left = tf.cast(features["left"], tf.int32)
        bottom = tf.cast(features["bottom"], tf.int32)
        right = tf.cast(features["right"], tf.int32)

        shape = tf.shape(image)
        bounding_box = tf.divide(
            x=tf.stack([top, left, bottom, right]),
            y=tf.stack([shape[0], shape[1], shape[0], shape[1]])
        )

        offset, target, _ = tf.image.sample_distorted_bounding_box(
            image_size=shape,
            bounding_boxes=tf.reshape(
                tensor=tf.cast(
                    x=tf.clip_by_value(bounding_box, 0.0, 1.0),
                    dtype=tf.float32
                ),
                shape=[1, 1, -1]
            ),
            min_object_covered=1.0,
            aspect_ratio_range=[0.75, 1.33]
        )

        image = tf.image.crop_to_bounding_box(
            image=image,
            offset_height=offset[0],
            offset_width=offset[1],
            target_height=target[0],
            target_width=target[1]
        )

        image = tf.image.resize_images(image, self.image_size)

        image = tf.image.per_image_standardization(image)

        if self.data_format == "channels_first":
            image = tf.transpose(image, [2, 0, 1])

        label = tf.cast(features["label"], tf.int32)

        return image, label

import os
import glob2
import numpy as np
import pandas as pd
import tensorflow as tf
from skimage.io import imread


class ImageClassesRule_map:
    def __init__(self, dir, dir_rule="*", excludes=[]):
        raw_classes = [os.path.basename(ii) for ii in glob2.glob(os.path.join(dir, dir_rule))]
        self.raw_classes = sorted([ii for ii in raw_classes if ii not in excludes])
        self.classes_2_indices = {ii: id for id, ii in enumerate(self.raw_classes)}
        self.indices_2_classes = {vv: kk for kk, vv in self.classes_2_indices.items()}

    def __call__(self, image_name):
        raw_image_class = os.path.basename(os.path.dirname(image_name))
        return self.classes_2_indices[raw_image_class]


def pre_process_folder(data_path, image_names_reg=None, image_classes_rule=None):
    while data_path.endswith(os.sep):
        data_path = data_path[:-1]
    if not data_path.endswith(".npz"):
        dest_pickle = os.path.basename(data_path) + "_shuffle.npz"
    else:
        dest_pickle = data_path

    if os.path.exists(dest_pickle):
        aa = np.load(dest_pickle)
        if len(aa.keys()) == 2:
            image_names, image_classes, embeddings = aa["image_names"], aa["image_classes"], []
        else:
            # dataset with embedding values
            image_names, image_classes, embeddings = aa["image_names"], aa["image_classes"], aa["embeddings"]
        print(">>>> reloaded from dataset backup:", dest_pickle)
    else:
        if not os.path.exists(data_path):
            print(">>>> [Error] data_path not exists, data_path:", data_path)
            return [], [], [], 0, None
        if image_classes_rule is None:
            # image_classes_rule = default_image_classes_rule
            image_classes_rule = ImageClassesRule_map(data_path)
        if image_names_reg is None:
            image_names = glob2.glob(os.path.join(data_path, "*", "*.jpg"))
            image_names += glob2.glob(os.path.join(data_path, "*", "*.png"))
        else:
            image_names = glob2.glob(os.path.join(data_path, image_names_reg))
        image_names = np.random.permutation(image_names).tolist()
        image_classes = [image_classes_rule(ii) for ii in image_names]
        embeddings = np.array([])
        np.savez_compressed(dest_pickle, image_names=image_names, image_classes=image_classes)
    classes = np.max(image_classes) + 1 if len(image_classes) > 0 else 0
    return image_names, image_classes, embeddings, classes, dest_pickle


def tf_imread(file_path):
    # tf.print('Reading file:', file_path)
    img = tf.io.read_file(file_path)
    # img = tf.image.decode_jpeg(img, channels=3)  # [0, 255]
    img = tf.image.decode_image(img, channels=3, expand_animations=False)  # [0, 255]
    img = tf.cast(img, "float32")  # [0, 255]
    return img


class RandomProcessImage:
    def __init__(self, img_shape=(112, 112), random_status=2, random_crop=None):
        self.img_shape, self.random_status, self.random_crop = img_shape[:2], random_status, random_crop
        if random_status >= 100:
            magnitude = 5 * random_status / 100
            print(">>>> RandAugment: magnitude =", magnitude)

            # from keras_cv_attention_models.imagenet import augment
            # translate_const, cutout_const = min(img_shape) * 0.45, 30
            # aa = augment.RandAugment(magnitude=magnitude, translate_const=translate_const, cutout_const=cutout_const)
            # aa.available_ops = ["AutoContrast", "Equalize", "ColorIncreasing", "ContrastIncreasing", "BrightnessIncreasing", "SharpnessIncreasing", "Cutout"]
            # self.process = lambda img: aa(tf.image.random_flip_left_right(img))
            import augment

            aa = augment.RandAugment(magnitude=magnitude, cutout_const=40)
            aa.available_ops = ["AutoContrast", "Equalize", "Color", "Contrast", "Brightness", "Sharpness", "Cutout"]
            self.process = lambda img: aa.distort(tf.image.random_flip_left_right(img))
        else:
            self.process = lambda img: self.tf_buildin_image_random(img)

    def tf_buildin_image_random(self, img):
        if self.random_status >= 0:
            img = tf.image.random_flip_left_right(img)
        if self.random_status >= 1:
            # 12.75 == 255 * 0.05
            img = tf.image.random_brightness(img, 12.75 * self.random_status)
        if self.random_status >= 2:
            img = tf.image.random_contrast(img, 1 - 0.1 * self.random_status, 1 + 0.1 * self.random_status)
            img = tf.image.random_saturation(img, 1 - 0.1 * self.random_status, 1 + 0.1 * self.random_status)
        if self.random_status >= 3 and self.random_crop is not None:
            img = tf.image.random_crop(img, self.random_crop)
        if img.shape[:2] != self.img_shape:
            img = tf.image.resize(img, self.img_shape)

        if self.random_status >= 1:
            img = tf.clip_by_value(img, 0.0, 255.0)
        return img


def sample_beta_distribution(size, concentration_0=0.4, concentration_1=0.4):
    gamma_1_sample = tf.random.gamma(shape=[size], alpha=concentration_1)
    gamma_2_sample = tf.random.gamma(shape=[size], alpha=concentration_0)
    return gamma_1_sample / (gamma_1_sample + gamma_2_sample)


def mixup(image, label, alpha=0.4):
    """Applies Mixup regularization to a batch of images and labels.

    [1] Hongyi Zhang, Moustapha Cisse, Yann N. Dauphin, David Lopez-Paz
    Mixup: Beyond Empirical Risk Minimization.
    ICLR'18, https://arxiv.org/abs/1710.09412
    """
    # mix_weight = tfp.distributions.Beta(alpha, alpha).sample([batch_size, 1])
    batch_size = tf.shape(image)[0]
    mix_weight = sample_beta_distribution(batch_size, alpha, alpha)
    mix_weight = tf.maximum(mix_weight, 1.0 - mix_weight)

    # Regard values with `> 0.9` as no mixup, this probability is near `1 - alpha`
    # alpha: no_mixup --> {0.2: 0.6714, 0.4: 0.47885, 0.6: 0.35132, 0.8: 0.26354, 1.0: 0.19931}
    mix_weight = tf.where(mix_weight > 0.9, tf.ones_like(mix_weight), mix_weight)

    label_mix_weight = tf.cast(tf.expand_dims(mix_weight, -1), "float32")
    img_mix_weight = tf.cast(tf.reshape(mix_weight, [batch_size, 1, 1, 1]), image.dtype)

    shuffle_index = tf.random.shuffle(tf.range(batch_size))
    image = image * img_mix_weight + tf.gather(image, shuffle_index) * (1.0 - img_mix_weight)
    label = tf.cast(label, "float32")
    label = label * label_mix_weight + tf.gather(label, shuffle_index) * (1 - label_mix_weight)
    return image, label


def pick_by_image_per_class(image_classes, image_per_class):
    cc = pd.value_counts(image_classes)
    class_pick = cc[cc >= image_per_class].index
    return np.array([ii in class_pick for ii in image_classes]), class_pick


class MXNetRecordGen:
    def __init__(self, data_path):
        import mxnet as mx

        self.mx = mx
        idx_path = os.path.join(data_path, "train.idx")
        bin_path = os.path.join(data_path, "train.rec")

        print(">>>> idx_path = %s, bin_path = %s" % (idx_path, bin_path))
        imgrec = mx.recordio.MXIndexedRecordIO(idx_path, bin_path, "r")
        rec_header, _ = mx.recordio.unpack(imgrec.read_idx(0))
        total_images = int(rec_header.label[0]) - 1
        classes = int(rec_header.label[1] - rec_header.label[0])
        self.imgrec, self.rec_header, self.classes, self.total_images = imgrec, rec_header, classes, total_images

    def __call__(self):
        while True:
            for ii in range(1, int(self.rec_header.label[0])):
                img_info = self.imgrec.read_idx(ii)
                header, img = self.mx.recordio.unpack(img_info)
                img_class = int(np.sum(header.label))

                label = tf.one_hot(img_class, depth=self.classes, dtype=tf.int32)
                img = tf.image.decode_jpeg(img, channels=3)
                img = tf.image.convert_image_dtype(img, tf.float32)
                yield img, label


def show_batch_sample(ds, rows=8, basic_size=1):
    import matplotlib.pyplot as plt

    aa, bb = ds.as_numpy_iterator().next()
    aa = aa / 2 + 0.5
    columns = aa.shape[0] // 8
    fig = plt.figure(figsize=(columns * basic_size, rows * basic_size))
    plt.imshow(np.vstack([np.hstack(aa[ii * columns : (ii + 1) * columns]) for ii in range(rows)]))
    plt.axis("off")
    plt.tight_layout()
    return fig


def partial_fc_split_pick(image_names, image_classes, batch_size, split=2, debug=False):
    total = len(image_classes)
    classes = np.max(image_classes) + 1
    splits = np.array([classes // split * ii for ii in range(split + 1)])  # Drop class if cannot divided, keep output shape concurrent

    shuffle_indexes = np.random.permutation(total)
    image_names, image_classes = image_names[shuffle_indexes], image_classes[shuffle_indexes]

    picks = [np.logical_and(image_classes >= splits[ii], image_classes < splits[ii + 1]) for ii in range(split)]
    if debug:
        print(">>>> splits:", splits, ", total images in each split:", [ii.sum() for ii in picks])

    indexes = np.arange(len(image_classes))
    split_index = [indexes[ii][: ii.sum() // batch_size * batch_size].reshape(-1, batch_size) for ii in picks]
    if debug:
        print(">>>> After drop remainder:", [ii.shape for ii in split_index], ", prod:", [np.prod(ii.shape) for ii in split_index])
    split_index = np.vstack(split_index)
    np.random.shuffle(split_index)  # in place shuffle
    split_index = split_index.ravel()  # flatten

    """ Test """
    if debug:
        bb = image_classes[split_index]
        rrs = []
        for ii in range(bb.shape[0] // batch_size):
            batch = bb[ii * batch_size : (ii + 1) * batch_size]
            split_id = np.argmax(batch[0] < splits[1:])
            rrs.append(np.alltrue(np.logical_and(batch >= splits[split_id], batch < splits[split_id + 1])))
        print(">>>> Total batches:", bb.shape[0] // batch_size, ", correctly split:", np.sum(rrs))

    return image_names[split_index], image_classes[split_index]


def partial_fc_split_gen(image_names, image_classes, batch_size, split=2, debug=False):
    while True:
        for image_name, image_class in zip(*partial_fc_split_pick(image_names, image_classes, batch_size, split, debug)):
            yield (image_name, image_class)


def prepare_dataset(
    data_path,
    image_names_reg=None,
    image_classes_rule=None,
    batch_size=128,
    img_shape=(112, 112),
    random_status=0,
    random_crop=(100, 100, 3),
    random_cutout_mask_area=0.0,
    mixup_alpha=0,
    image_per_class=0,
    partial_fc_split=0,
    cache=False,
    shuffle_buffer_size=None,
    is_train=True,
    teacher_model_interf=None,
):
    AUTOTUNE = tf.data.experimental.AUTOTUNE
    image_names, image_classes, embeddings, classes, _ = pre_process_folder(data_path, image_names_reg, image_classes_rule)
    total_images = len(image_names)
    if total_images == 0:
        print(">>>> [Error] total_images is 0, image_names:", image_names, "image_classes:", image_classes)
        return None, None
    print(">>>> Image length: %d, Image class length: %d, classes: %d" % (len(image_names), len(image_classes), classes))
    if image_per_class != 0:
        pick, class_pick = pick_by_image_per_class(image_classes, image_per_class)
        image_names, image_classes = image_names[pick], image_classes[pick]
        total_images = len(image_names)
        if len(embeddings) != 0:
            embeddings = embeddings[pick]
        print(">>>> After pick[%d], images: %d, valid classes: %d" % (image_per_class, len(image_names), class_pick.shape[0]))

    if len(embeddings) != 0 and teacher_model_interf is None:
        # dataset with embedding values
        print(">>>> embeddings: %s. This takes some time..." % (np.shape(embeddings),))
        ds = tf.data.Dataset.from_tensor_slices((image_names, embeddings, image_classes)).shuffle(buffer_size=total_images)
        process_func = lambda imm, emb, label: (tf_imread(imm), (emb, tf.one_hot(label, depth=classes, dtype=tf.int32)))
    elif partial_fc_split != 0:
        print(">>>> partial_fc_split provided:", partial_fc_split)
        picked_images, _ = partial_fc_split_pick(image_names, image_classes, batch_size, split=partial_fc_split, debug=True)
        total_images = picked_images.shape[0]
        sub_classes = classes // partial_fc_split
        print(">>>> total images after pick: {}, sub_classes: {}".format(total_images, sub_classes))

        gen_func = lambda: partial_fc_split_gen(image_names, image_classes, batch_size, split=partial_fc_split)
        output_signature = (tf.TensorSpec(shape=(), dtype=tf.string), tf.TensorSpec(shape=(), dtype=tf.int64))
        ds = tf.data.Dataset.from_generator(gen_func, output_signature=output_signature)
        process_func = lambda imm, label: (tf_imread(imm), tf.one_hot(label % sub_classes, depth=sub_classes, dtype=tf.int32))
    else:
        ds = tf.data.Dataset.from_tensor_slices((image_names, image_classes)).shuffle(buffer_size=total_images)
        process_func = lambda imm, label: (tf_imread(imm), tf.one_hot(label, depth=classes, dtype=tf.int32))

    ds = ds.map(process_func, num_parallel_calls=AUTOTUNE)

    if random_cutout_mask_area > 0:
        print(">>>> random_cutout_mask_area provided:", random_cutout_mask_area)
        # mask_height = img_shape[0] * 2 // 5
        random_height = lambda: tf.random.uniform((), int(img_shape[0] * 0.55), int(img_shape[0] * 0.7), dtype=tf.int32)
        mask_func = lambda imm, label: (
            tf.cond(
                tf.random.uniform(()) < random_cutout_mask_area,
                # lambda: tf.concat([imm[:-mask_height], tf.zeros_like(imm[-mask_height:]) + 128], axis=0),
                lambda: tf.image.pad_to_bounding_box(imm[:random_height()] - 128, 0, 0, img_shape[0], img_shape[1]) + 128,
                lambda: imm,
            ),
            label,
        )
        ds = ds.map(mask_func, num_parallel_calls=AUTOTUNE)

    if is_train and random_status >= 0:
        random_process_image = RandomProcessImage(img_shape, random_status, random_crop)
        random_process_func = lambda xx, yy: (random_process_image.process(xx), yy)
        ds = ds.map(random_process_func, num_parallel_calls=AUTOTUNE)

    ds = ds.batch(batch_size, drop_remainder=True)  # Use batch --> map has slightly effect on dataset reading time, but harm the randomness
    if mixup_alpha > 0 and mixup_alpha <= 1:
        print(">>>> mixup_alpha provided:", mixup_alpha)
        ds = ds.map(lambda xx, yy: mixup((xx - 127.5) * 0.0078125, yy, alpha=mixup_alpha))
    else:
        ds = ds.map(lambda xx, yy: ((xx - 127.5) * 0.0078125, yy))

    if teacher_model_interf is not None:
        if teacher_model_interf.output_shape[-1] == classes:
            print(">>>> KLDivergence teacher model interface provided.")
            emb_func = lambda imm, label: (imm, teacher_model_interf(imm))
            ds = ds.map(emb_func, num_parallel_calls=AUTOTUNE)
        else:
            print(">>>> Teacher model interface provided.")
            emb_func = lambda imm, label: (imm, (teacher_model_interf(imm), label))
            ds = ds.map(emb_func, num_parallel_calls=AUTOTUNE)

    if partial_fc_split != 0:
        # Attanch classes in inputs for picking sub NormDense header
        ds = ds.map(lambda imm, label: ((imm, tf.argmax(label, axis=-1, output_type=tf.int32)), label), num_parallel_calls=AUTOTUNE)

    ds = ds.prefetch(buffer_size=AUTOTUNE)
    steps_per_epoch = int(np.floor(total_images / float(batch_size)))
    # steps_per_epoch = len(ds)
    return ds, steps_per_epoch


def prepare_distill_dataset_tfrecord(data_path, batch_size=128, img_shape=(112, 112), random_status=2, random_crop=(100, 100, 3), **kw):
    AUTOTUNE = tf.data.experimental.AUTOTUNE
    decode_base_info = {
        "classes": tf.io.FixedLenFeature([], dtype=tf.int64),
        "emb_shape": tf.io.FixedLenFeature([], dtype=tf.int64),
        "total": tf.io.FixedLenFeature([], dtype=tf.int64),
        "use_fp16": tf.io.FixedLenFeature([], dtype=tf.int64),
    }
    decode_feature = {
        "image_names": tf.io.FixedLenFeature([], dtype=tf.string),
        "image_classes": tf.io.FixedLenFeature([], dtype=tf.int64),
        # "embeddings": tf.io.FixedLenFeature([emb_shape], dtype=tf.float32),
        "embeddings": tf.io.FixedLenFeature([], dtype=tf.string),
    }

    # base info saved in the first data line
    header = tf.data.TFRecordDataset([data_path]).as_numpy_iterator().next()
    hh = tf.io.parse_single_example(header, decode_base_info)
    classes, emb_shape, total = hh["classes"].numpy(), hh["emb_shape"].numpy(), hh["total"].numpy()
    use_fp16 = hh["use_fp16"].numpy()
    emb_dtype = tf.float16 if use_fp16 else tf.float32
    print(">>>> [Base info] total:", total, "classes:", classes, "emb_shape:", emb_shape, "use_fp16:", use_fp16)

    random_process_image = RandomProcessImage(img_shape, random_status, random_crop)

    def decode_fn(record_bytes):
        ff = tf.io.parse_single_example(record_bytes, decode_feature)
        image_name, image_classe, embedding = ff["image_names"], ff["image_classes"], ff["embeddings"]
        img = random_process_image.process(tf_imread(image_name))
        label = tf.one_hot(image_classe, depth=classes, dtype=tf.int32)
        embedding = tf.io.decode_raw(embedding, emb_dtype)
        embedding.set_shape([emb_shape])
        return img, (embedding, label)

    ds = tf.data.TFRecordDataset([data_path])
    ds = ds.shuffle(buffer_size=batch_size * 1000).repeat()
    ds = ds.map(decode_fn, num_parallel_calls=AUTOTUNE)
    ds = ds.batch(batch_size, drop_remainder=True)
    ds = ds.map(lambda xx, yy: ((xx - 127.5) * 0.0078125, yy))
    ds = ds.prefetch(buffer_size=AUTOTUNE)
    steps_per_epoch = int(np.floor(total / float(batch_size)))
    return ds, steps_per_epoch


class Triplet_dataset:
    def __init__(
        self,
        data_path,
        image_names_reg=None,
        image_classes_rule=None,
        batch_size=48,
        image_per_class=4,
        img_shape=(112, 112, 3),
        random_status=3,
        random_crop=(100, 100, 3),
        teacher_model_interf=None,
        **kw,
    ):
        AUTOTUNE = tf.data.experimental.AUTOTUNE
        self.image_classes_rule = ImageClassesRule_map(data_path) if image_classes_rule is None else image_classes_rule
        image_names, image_classes, embeddings, classes, _ = pre_process_folder(data_path, image_names_reg, self.image_classes_rule)
        image_per_class = max(4, image_per_class)
        pick, _ = pick_by_image_per_class(image_classes, image_per_class)
        image_names, image_classes = image_names[pick].astype(str), image_classes[pick]
        self.classes = classes

        image_dataframe = pd.DataFrame({"image_names": image_names, "image_classes": image_classes})
        self.image_dataframe = image_dataframe.groupby("image_classes").apply(lambda xx: xx.image_names.values)
        self.split_func = lambda xx: np.array(np.split(np.random.permutation(xx)[: len(xx) // image_per_class * image_per_class], len(xx) // image_per_class))
        self.image_per_class = image_per_class
        self.batch_size = batch_size // image_per_class * image_per_class
        self.img_shape = img_shape[:2]
        self.channels = img_shape[2] if len(img_shape) > 2 else 3
        print("The final train_dataset batch will be %s" % ([self.batch_size, *self.img_shape, self.channels]))

        one_hot_label = lambda label: tf.one_hot(label, depth=classes, dtype=tf.int32)
        random_process_image = RandomProcessImage(img_shape, random_status, random_crop)
        random_imread = lambda imm: random_process_image.process(tf_imread(imm))
        if len(embeddings) != 0 and teacher_model_interf is None:
            self.teacher_embeddings = dict(zip(image_names, embeddings[pick]))
            emb_spec = tf.TensorSpec(shape=(embeddings.shape[-1],), dtype=tf.float32)
            output_signature = (tf.TensorSpec(shape=(), dtype=tf.string), emb_spec, tf.TensorSpec(shape=(), dtype=tf.int64))
            ds = tf.data.Dataset.from_generator(self.image_shuffle_gen_with_emb, output_signature=output_signature)
            process_func = lambda imm, emb, label: (random_imread(imm), (emb, one_hot_label(label)))
        else:
            output_signature = (tf.TensorSpec(shape=(), dtype=tf.string), tf.TensorSpec(shape=(), dtype=tf.int64))
            ds = tf.data.Dataset.from_generator(self.image_shuffle_gen, output_signature=output_signature)
            process_func = lambda imm, label: (random_imread(imm), one_hot_label(label))
        ds = ds.map(process_func, num_parallel_calls=AUTOTUNE)

        ds = ds.batch(self.batch_size, drop_remainder=True)
        if teacher_model_interf is not None:
            print(">>>> Teacher model interference provided.")
            emb_func = lambda imm, label: (imm, (teacher_model_interf(imm), label))
            ds = ds.map(emb_func, num_parallel_calls=AUTOTUNE)

        ds = ds.map(lambda xx, yy: ((xx - 127.5) * 0.0078125, yy))
        self.ds = ds.prefetch(buffer_size=AUTOTUNE)

        shuffle_dataset = self.image_dataframe.map(self.split_func)
        self.total = np.vstack(shuffle_dataset.values).flatten().shape[0]
        self.steps_per_epoch = int(np.floor(self.total / float(batch_size)))

    def image_shuffle_gen(self):
        while True:
            tf.print("Shuffle image data...")
            shuffle_dataset = self.image_dataframe.map(self.split_func)
            image_data = np.random.permutation(np.vstack(shuffle_dataset.values)).flatten()
            for ii in image_data:
                yield (ii, self.image_classes_rule(ii))
            # return ((ii, int(ii.split(os.path.sep)[-2])) for ii in image_data)

    def image_shuffle_gen_with_emb(self):
        while True:
            tf.print("Shuffle image with embedding data...")
            shuffle_dataset = self.image_dataframe.map(self.split_func)
            image_data = np.random.permutation(np.vstack(shuffle_dataset.values)).flatten()
            for ii in image_data:
                yield (ii, self.teacher_embeddings[ii], self.image_classes_rule(ii))
            # return ((ii, self.teacher_embeddings[ii], int(ii.split(os.path.sep)[-2])) for ii in image_data)

import torch
import torch.nn as nn
import DataAugments as da
from skimage.color import rgb2lab
import BackBoneModel as bbm


class Rotation:
    """
    inspired by https://arxiv.org/abs/1803.07728
    """

    def __init__(self, embed_features, task_head, device="cpu"):
        """

        :param embed_features:
        :param task_head:
        :param device:
        """
        self.name = 'Rotation'
        self.device = device

        self.task_head = task_head(embed_features, 4, device)
        self.loss = nn.CrossEntropyLoss()
        self.labels = None

    def pretreat(self, input_data):
        """
        rotate the image by a random angle and save the angle as a label
        :param input_data: a batch of raw images in np array
        :return: the rotated images as tensors
        """

        rotated, self.labels = da.rotate(input_data)

        rotated = torch.tensor(rotated, dtype=torch.float, device=self.device, requires_grad=True)
        self.labels = torch.as_tensor(self.labels, dtype=torch.long, device=self.device)

        return rotated

    def generate_loss(self, embedded_data, clear_labels=True):
        """

        :param embedded_data:
        :param clear_labels:
        :return:
        """

        predictions = self.task_head.forward(embedded_data)
        loss = self.loss(predictions, self.labels)

        if clear_labels:
            self.labels = None

        return loss


class Colorization:
    """
    inspired by https://arxiv.org/abs/1603.08511
    """

    def __init__(self, embed_features, task_head, device="cpu"):
        """

        :param embed_features:
        :param task_head:
        :param device:
        """
        self.name = 'Colorization'
        self.device = device

        self.desired_precision = 128

        self.harmonization = nn.Conv2d(1, 3, (1, 1), device=device)
        self.task_head = task_head(embed_features, self.desired_precision * 2, device)

        self.loss = nn.CrossEntropyLoss()
        self.labels = None

    def pretreat(self, input_data):
        """

        :param input_data: a batch of raw images
        :return:
        """

        input_data = input_data / 255  # skimage expects float values to be between -1 and 1 or 0 and 1 I opt for [0, 1]
        # convert to lab and maintain (b, c, h, w) shape
        lab_data = rgb2lab(input_data.transpose(0, 2, 3, 1)) / 110  # source paper uses this 110 normalization value
        lab_data = torch.tensor(lab_data.transpose(0, 3, 1, 2), dtype=torch.float, device=self.device)
        l_data = lab_data[:, 0, :, :].unsqueeze(1)
        self.labels = lab_data[:, 1:, :, :]

        return self.harmonization(l_data)

    def generate_loss(self, embedded_data, clear_labels=True):
        """

        :param embedded_data:
        :param clear_labels:
        :return:
        """

        output_shape = list(self.labels.shape)
        output_shape[1] = self.desired_precision * 2

        # labels are between -1 and 1
        # turning ab channel values into indices for label values to be used in cross entropy
        self.labels = nn.functional.sigmoid(self.labels)
        self.labels = (self.labels * self.desired_precision) // 1

        # up-scaling the embedded data into ab value predictions for each pixel
        output = self.task_head.forward(embedded_data, output_shape)

        # generate for a and b values
        loss_a = self.loss(output[:, :self.desired_precision], self.labels[:, 0].long())
        loss_b = self.loss(output[:, self.desired_precision:], self.labels[:, 1].long())

        loss = (loss_a + loss_b) / 2

        if clear_labels:
            self.labels = None

        return loss


class Contrastive:
    """
    inspired by https://arxiv.org/abs/2002.05709
    """

    def __init__(self, embed_features, task_head, device="cpu"):
        """

        :param embed_features:
        """
        self.name = "Contrastive"
        self.device = device

        # "As shown in Section 3, the combination of random crop and
        # color distortion is crucial to achieve a good performance"

        self.augments = [da.horizontal_flip, da.cropping, da.color_distortions, da.gauss_blur]  # maybe masking cutting

        self.task_head = task_head(embed_features, 256, device)
        self.temperature = 0.1

    def pretreat(self, input_data):
        """

        :param input_data:
        :return:
        """

        # todo:
        #  gauss shows bad results in their augmentation heatmap,
        #  but cutout shows good performance so maybe use the masking augmentation
        #  potentially ideal augment order hflip, crop, color_distort, blur, mask

        aug_data_1 = input_data.copy()
        for augment in self.augments:  # I was expecting the data to stay as a np.array, but it doesn't
            aug_data_1, scrap_info = augment(aug_data_1)

        aug_data_2 = input_data.copy()
        for augment in self.augments:
            aug_data_2, scrap_info = augment(aug_data_2)

        aug_data = torch.concatenate((aug_data_1, aug_data_2), dim=0).float().requires_grad_(True).to(self.device)

        return aug_data

    def generate_loss(self, embedded_data, clear_labels=True):
        """

        :param embedded_data:
        :param clear_labels:
        :return:
        """
        """
        def nt_xent(out1, out2, temp):
            out = torch.concatenate((out1, out2), dim=0)
            n_samples = out.shape[0]

            sim = torch.matmul(out, out.T)
            scaled_sim = torch.exp(sim / temp)

            mask = ~torch.eye(n_samples)
            neg = torch.sum(scaled_sim * mask, dim=-1)
            
            pos = torch.exp(torch.sum(out1 * out2, dim=-1) / temp)
            pos = torch.concatenate((pos, pos), dim=0)
            
            loss = -torch.log(pos / neg).mean()
            return loss"""

        output = self.task_head(embedded_data)

        aug1 = nn.functional.normalize(output[:output.shape[0] // 2])
        aug2 = nn.functional.normalize(output[output.shape[0] // 2:])
        out = torch.concatenate((aug1, aug2), dim=0)
        n_samples = out.shape[0]

        sim = torch.matmul(out, out.T)
        scaled_sim = torch.exp(sim / self.temperature)

        mask = ~torch.eye(n_samples, dtype=torch.bool, device=self.device)
        neg = torch.sum(scaled_sim * mask, dim=-1)

        pos = torch.exp(torch.sum(aug1 * aug2, dim=-1) / self.temperature)
        pos = torch.concatenate((pos, pos), dim=0)

        loss = -torch.log(pos / neg).mean()

        return loss


class MaskedAutoEncoding:
    """
    inspired by https://arxiv.org/abs/2111.06377
    """

    def __init__(self, embed_features, task_head, device="cpu"):
        """

        :param embed_features:
        :param task_head:
        :param device:
        """
        self.name = "Masked Auto Encoding"
        self.device = device

        self.harmonization = nn.Conv2d(4, 3, (1, 1), device=device)
        self.task_head = task_head(embed_features, 3, device)

        self.loss = nn.MSELoss()
        self.labels = None

    def pretreat(self, input_data):
        """

        :param input_data:
        :return:
        """

        # todo:
        #  normalizing the input is helpful
        #  augmentations have been shown to be potentially helpful but not necessary
        #  no color jitter, yes cropping and horizontal flipping

        input_data = input_data / 255
        masked_image, mask = da.masking(input_data)

        self.labels = torch.tensor(mask, dtype=torch.float, device=self.device, requires_grad=True), \
                      torch.tensor(input_data, dtype=torch.float, device=self.device, requires_grad=True)

        combo = torch.concatenate((torch.tensor(masked_image, dtype=torch.float, device=self.device),
                                   self.labels[0]), dim=1).requires_grad_(True)

        pretreated = self.harmonization(combo)

        return pretreated

    def generate_loss(self, embedded_data, clear_labels=True):
        """

        :param embedded_data:
        :param clear_labels:
        :return:
        """

        output_shape = self.labels[1].shape

        output = self.task_head.forward(embedded_data, output_shape)
        output = nn.functional.sigmoid(output)

        result = self.loss(output * self.labels[0], self.labels[1] * self.labels[0])

        if clear_labels:
            self.labels = None

        return result


class Cifar10Classification:
    """

    """

    def __init__(self, embed_dim, task_head, device="cpu"):
        """

        :param embed_dim:
        :param task_head:
        :param device:
        """
        self.name = "Cifar10 Classification"
        self.device = device

        self.task_head = task_head(embed_dim, 10, device)
        self.loss = nn.CrossEntropyLoss()

    def pretreat(self, input_data):
        return torch.tensor(input_data, dtype=torch.float, device=self.device, requires_grad=True) / 255

    def generate_loss(self, embed_data, labels):
        """

        :param embed_data:
        :param labels:
        :return:
        """
        prediction = self.task_head.forward(embed_data)

        loss = self.loss(prediction, labels.to(self.device))

        return loss


# todo (once ready for full scale testing) flesh out multi-task Task
class AllFourSSL:
    """

    """

    def __init__(self, embed_features, task_head):
        """

        :param embed_features:
        :param task_head:
        """
        self.tasks = [Rotation(embed_features, bbm.SimpleTaskHead),  # defining model objects
                      Colorization(embed_features, bbm.SimpleConvDecode),
                      Contrastive(embed_features, bbm.SimpleTaskHead),
                      MaskedAutoEncoding(embed_features, bbm.SimpleConvDecode)]

        self.params = []
        for task in self.tasks:
            try:  # some tasks require data modification that requires "harmonization" with the original data format
                self.params += list(task.harmonization.parameters()) + list(task.task_head.parameters())
            except AttributeError:
                self.params += list(task.task_head.parameters())

    def pretreat(self, input_data):
        """

        :param input_data:
        :return:
        """
        # batch = [task.pretreat(data["X_train"][indices[i - batch_size:i]]) for task in tasks]

    def generate_loss(self, embedded_data, clear_labels=True):
        """

        :param embedded_data:
        :param clear_labels:
        :return:
        """

        """total_loss = []
        first_index = 0  # tasks like contrastive change the batch size, so I count through each batch length
        for k in range(len(batch)):
            try:
                loss = tasks[k].generate_loss(latent[first_index:first_index + batch[k].shape[0]])
            except TypeError:  # ready for a supervised training task
                loss = tasks[k].generate_loss(latent[first_index:first_index + batch[k].shape[0]],
                                              labels)
            total_loss.append(loss)

            first_index += batch[k].shape[0]

        averaged_task_loss = torch.mean(torch.tensor(total_loss, requires_grad=True))"""

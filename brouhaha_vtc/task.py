import itertools
from re import T

from typing import Dict, Sequence, Text, Tuple, Union

import torch
from pyannote.database import Protocol
from torch_audiomentations.core.transforms_interface import BaseWaveformTransform
from torchmetrics import Metric, MeanSquaredError

from pyannote.audio.core.task import Problem, Resolution, Specifications, Task
from pyannote.audio.tasks.segmentation.mixins import SegmentationTaskMixin
from pyannote.audio.core.io import AudioFile
from pyannote.core import Segment, SlidingWindowFeature



from pyannote.audio.utils.loss import binary_cross_entropy, mse_loss

import numpy as np



class RegressiveActivityDetectionTask(SegmentationTaskMixin, Task):
    # TODO

    # TODO: look into `default_loss` and `setup_loss_func` for the task
    # Look into batch related function (batch, chunck, collate...)
    # Chunck : original method (super or surcharge)
    # Loss : BCE for VAD, MSE for C50 and SNR
    # Loss_vad + lambda Loss_snr + lambda Loss_c50 -> pour l'instant que des 1, on fera un gridsearch
    # Create a mask for snr loss when there is no speech
    # We want to log all the losses
    # apply mask to the linear_snr & to Y

    # Before the 8th
    # Task
    # Model
    # Database

    # For the 8th, first graphs that show losses for VAD, snr, c50 that decreases
    # for now, let's forget about the masks :
    # Hadrien says you can harass him if you're stuck!
    # Bon courage!
    def __init__(
        self,
        protocol: Protocol,
        duration: float = 2.0,
        warm_up: Union[float, Tuple[float, float]] = 0.0,
        balance: Text = None,
        weight: Text = None,
        batch_size: int = 32,
        num_workers: int = None,
        pin_memory: bool = False,
        augmentation: BaseWaveformTransform = None,
        metric: Union[Metric, Sequence[Metric], Dict[str, Metric]] = None,
    ):

        super().__init__(
            protocol,
            duration=duration,
            warm_up=warm_up,
            batch_size=batch_size,
            num_workers=num_workers,
            pin_memory=pin_memory,
            augmentation=augmentation,
            metric=metric,
        )

        self.balance = balance
        self.weight = weight
        self.first_loss_snr = 1
        self.first_loss_c50 = 1
        self.first_losses_c50 = []
        self.first_losses_snr = []

        self.specifications = Specifications(
            problem=Problem.MULTI_LABEL_CLASSIFICATION,
            resolution=Resolution.FRAME,
            duration=self.duration,
            warm_up=self.warm_up,
            classes=[
                "speech",
            ],
        )

    def collate_y(self, batch) -> torch.Tensor:
        """Collate the speakers detection, snr labels and c50 labels
        into one target tensor

        Parameters
        ----------
        batch : (batch_size) list of dict
            Dictionaries with the following keys:
            X : np.ndarray
                Audio chunk as (num_samples, num_channels) array.
            y : SlidingWindowFeature
                Frame-wise (labels snr c50) as (num_frames, num_labels + 2) array.

        Returns
        -------
        Y: (batch_size, num_frames, num_speakers + 2) tensor
            One-hot-encoding of current chunk speaker activity, snr label and c50 label:
                * one_hot_y[b, f, s] = 1 if sth speaker is active at fth frame
                * one_hot_y[b, f, s] = 0 otherwise.
                * one_hot_y[b, f, -2] = snr_label
                * one_hot_y[b, f, -1] = c50_label
        """
        # gather common set of labels
        # b["y"] is a SlidingWindowFeature instance
        labels = sorted(set(itertools.chain(*(b["y"].labels for b in batch))))

        batch_size, num_frames, num_labels = (
            len(batch),
            len(batch[0]["y"]),
            len(labels),
        )
        Y = np.zeros((batch_size, num_frames, num_labels + 2))

        for i, b in enumerate(batch):
            for local_idx, label in enumerate(b["y"].labels):
                global_idx = labels.index(label)
                Y[i, :, global_idx] = b["y"].data[:, local_idx]
            Y[i, :, -2:] = b["y"].data[:, -2:]

        return torch.from_numpy(Y)

    def adapt_y(self, collated_y: torch.Tensor) -> torch.Tensor:
        """Get voice activity detection targets, snr_label targets
        and c50_label targets

        Parameters
        ----------
        collated_y : (batch_size, num_frames, num_speakers + 2) tensor
            One-hot-encoding of current chunk speaker activity:
                * one_hot_y[b, f, s] = 1 if sth speaker is active at fth frame
                * one_hot_y[b, f, s] = 0 otherwise.
                * one_hot_y[b, f, -2] = snr_label
                * one_hot_y[b, f, -1] = c50_label

        Returns
        -------
        y : (batch_size, num_frames, 3) tensor
            y[b, f, 0] = 1 if at least one speaker is active at fth frame, 0 otherwise.
            y[b, f, 1] = snr_label
            y[b, f, 2] = c50_label
        """
        speaker_feat = 1 * (torch.sum(collated_y[:,:,:-2], dim=2, keepdims=False) > 0)
        snr_feat = collated_y[:,:,-2]
        c50_feat = collated_y[:,:,-1]
        return torch.stack((speaker_feat, snr_feat, c50_feat), dim=2)

    def prepare_chunk(
        self,
        file: AudioFile,
        chunk: Segment,
        duration: float = None
    ) -> dict:
        """Extract audio chunk and corresponding frame-wise labels

        Parameters
        ----------
        file : AudioFile
            Audio file.
        chunk : Segment
            Audio chunk.
        duration : float, optional
            Fix chunk duration to avoid rounding errors. Defaults to self.duration

        Returns
        -------
        sample : dict
            Dictionary with the following keys:
            X : np.ndarray
                Audio chunk as (num_samples, num_channels) array.
            y : SlidingWindowFeature
                Frame-wise (labels snr c50) as (num_frames, num_labels) array.

        """

        sample = dict()

        # read (and resample if needed) audio chunk
        duration = duration or self.duration
        sample["X"], _ = self.model.audio.crop(file, chunk, duration=duration)

        # use model introspection to predict how many frames it will output
        num_samples = sample["X"].shape[1]
        num_frames, _ = self.model.introspection(num_samples)
        resolution = duration / num_frames

        # discretize annotation, using model resolution
        annotations = file["annotation"].discretize(
            support=chunk, resolution=resolution, duration=duration
        )

        snr = file['target_features']['snr'].align(to=annotations)
        score = file['target_features']['c50']
        data = np.concatenate((annotations.data, snr.data, np.full(snr.data.shape, score)), axis=1)

        sample['y'] = SlidingWindowFeature(data, annotations.sliding_window, labels = annotations.labels)

        return sample
    

    def set_first_losses(
        self, specifications: Specifications, target, prediction, weight=None
    ) -> torch.Tensor:
        """Compute and set the first snr_loss and c50_loss for normalization
        """
        print("settings the first losses")
        self.first_losses_snr.append(float(mse_loss(prediction[:,:,1].unsqueeze(dim=2), target[:,:,1], weight=weight)))
        self.first_losses_c50.append(float(mse_loss(prediction[:,:,2].unsqueeze(dim=2), target[:,:,2], weight=weight)))

        self.first_loss_snr = max(self.first_losses_snr)
        self.first_loss_c50 = max(self.first_losses_c50)


    def default_loss(
        self, specifications: Specifications, target, prediction, weight=None
    ) -> torch.Tensor:
        """Compute the specific loss for the RegressiveActivityDetectionTask
        Three separate losses are computed :
            * Binary cross-entropy for Voice Activity Detection
            * Mean Square Error for snr labels
            * Mean Square Error for c50 labels
        The global loss is the normalized mean of the three losses.

        Parameters
        ----------
        specifications : Specifications
            Task specifications
        target : torch.Tensor
            (batch_size, num_frames, 3)
            target[b, f, 0] = 1 if at least one speaker is active at fth frame, 0 otherwise.
            target[b, f, 1] = snr_label
            target[b, f, 2] = c50_label
        prediction : torch.Tensor
            (batch_size, num_frames, 3)
            prediction[b, f, 0] = 1 if at least one speaker is predicted active at fth frame, 0 otherwise.
            prediction[b, f, 1] = predicted snr_label
            prediction[b, f, 2] = predicted c50_label
        weight : torch.Tensor, optional
            (batch_size, num_frames, 1)

        Returns
        -------
        loss : torch.Tensor
            Normalized mean of the three following losses
        loss_vad : torch.Tensor
            Binary cross-entropy 
        loss_snr : torch.Tensor
            Mean Square Error normalized by the first value
        loss_c50 : torch.Tensor
            Mean Square Error normalized by the first value

        """
        lambda_1 = 1
        lambda_2 = 1
        loss_vad = binary_cross_entropy(prediction[:,:,0].unsqueeze(dim=2), target[:,:,0], weight=weight)
        loss_snr = mse_loss(prediction[:,:,1].unsqueeze(dim=2), target[:,:,1], weight=weight)
        loss_c50 = mse_loss(prediction[:,:,2].unsqueeze(dim=2), target[:,:,2], weight=weight)

        loss_snr = loss_snr / self.first_loss_snr
        loss_c50 = loss_c50 / self.first_loss_c50

        loss = loss_vad + lambda_1 * loss_snr + lambda_2 * loss_c50

        return loss, loss_vad, loss_snr, loss_c50
    
    def validation_step(self, batch, batch_idx: int):
        """Compute validation area under the ROC curve

        Parameters
        ----------
        batch : dict of torch.Tensor
            Current batch.
        batch_idx: int
            Batch index.
        """

        X, y = batch["X"], batch["y"]
        # X = (batch_size, num_channels, num_samples)
        # y = (batch_size, num_frames, num_classes) or (batch_size, num_frames)

        y_pred = self.model(X)
        _, num_frames, _ = y_pred.shape
        # y_pred = (batch_size, num_frames, num_classes)

        # - remove warm-up frames
        # - downsample remaining frames
        warm_up_left = round(self.warm_up[0] / self.duration * num_frames)
        warm_up_right = round(self.warm_up[1] / self.duration * num_frames)
        preds = y_pred[:, warm_up_left : num_frames - warm_up_right : 10]
        target = y[:, warm_up_left : num_frames - warm_up_right : 10]

        # for now we keep AUROC on VAD as validation metric
        self.model.validation_metric(
            preds[:,:,0].reshape(-1).int(),
            target[:,:,0].reshape(-1).int(),
        )

        self.model.log_dict(
            self.model.validation_metric,
            on_step=False,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )

    def training_step(self, batch, batch_idx: int):
        """Training step according specific to RegressiveActivityDetectionTask

        Custom loss : Normalized mean of BCE on VAD, MSE on SNR, MSE on c50

        If "weight" attribute exists, batch[self.weight] is also passed to the loss function
        during training (but has no effect in validation).

        Parameters
        ----------
        batch : (usually) dict of torch.Tensor
            Current batch.
        batch_idx: int
            Batch index.
        stage : {"train", "val"}
            "train" for training step, "val" for validation step

        Returns
        -------
        loss : {str: torch.tensor}
            {"loss": loss}
        """

        # forward pass
        y_pred = self.model(batch["X"])

        batch_size, num_frames, _ = y_pred.shape
        # (batch_size, num_frames, num_classes)

        # target
        y = batch["y"]

        # frames weight
        weight_key = getattr(self, "weight", None)
        weight = batch.get(
            weight_key,
            torch.ones(batch_size, num_frames, 1, device=self.model.device),
        )
        # (batch_size, num_frames, 1)

        # warm-up
        warm_up_left = round(self.warm_up[0] / self.duration * num_frames)
        weight[:, :warm_up_left] = 0.0
        warm_up_right = round(self.warm_up[1] / self.duration * num_frames)
        weight[:, num_frames - warm_up_right :] = 0.0

        # set first loss for snr and c50
        if self.model.current_epoch == 0 and batch_idx < 10:
            self.set_first_losses(self.specifications, y, y_pred, weight=weight)

        # compute loss
        losses = self.default_loss(self.specifications, y, y_pred, weight=weight)

        for loss, loss_type in zip(losses, ['Train', 'vad', 'snr', 'c50']):
            self.model.log( 
                f"{self.logging_prefix}{loss_type}Loss",
                loss,
                on_step=False,
                on_epoch=True,
                prog_bar=True,
                logger=True,
            )
        return {"loss": losses[0]}

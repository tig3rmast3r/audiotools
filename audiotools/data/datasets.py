from multiprocessing import Manager
from typing import List

import numpy as np
import torch
from torch.utils.data import SequentialSampler
from torch.utils.data.distributed import DistributedSampler

from ..core import AudioSignal
from ..core import util

# We need to set SHARED_KEYS statically, with no relationship to the
# BaseDataset object, or we'll hit RecursionErrors in the lookup.
SHARED_KEYS = ["duration", "transform", "sample_rate"]


class BaseDataset:
    """This BaseDataset class adds all the necessary logic so that there is
    a dictionary that is shared across processes when working with a
    DataLoader with num_workers > 0. It adds an attribute called
    `shared_dict`, and changes the getattr and setattr for the object
    so that it looks things up in the shared_dict if it's in the above
    SHARED_KEYS. The complexity here is coming from working around a few
    quirks in multiprocessing.
    """

    def __init__(self, length, **kwargs):
        super().__init__()
        self.length = length
        # The following snippet of code is how we share a
        # parameter across workers in a DataLoader, without
        # introducing syntax overhead upstream.

        # 1. We use a Manager object, which is shared between
        # dataset replicas that are passed to the workers.
        self.shared_dict = Manager().dict()

        # Instead of setting `self.duration = duration` for example, we
        # instead first set it inside the `self.shared_dict` object. Further
        # down, we'll make it so that `self.duration` still works, but
        # it works by looking up the key "duration" in `self.shared_dict`.
        for k, v in kwargs.items():
            if k in SHARED_KEYS:
                self.shared_dict[k] = v

        self.length = length

    def __getattribute__(self, name: str):
        # Look up the name in SHARED_KEYS (see above). If it's there,
        # return it from the dictionary that is kept in shared memory.
        # Otherwise, do the normal __getattribute__. This line only
        # runs if the key is in SHARED_KEYS.
        if name in SHARED_KEYS:
            return self.shared_dict[name]
        else:
            return super().__getattribute__(name)

    def __setattr__(self, name, value):
        # Look up the name in SHARED_KEYS (see above). If it's there
        # set the value in the dictionary accordingly, so that it the other
        # dataset replicas know about it. Otherwise, do the normal
        # __setattr__. This line only runs if the key is in SHARED_KEYS.
        if name in SHARED_KEYS:
            self.shared_dict[name] = value
        else:
            super().__setattr__(name, value)

    def __len__(self):
        return self.length

    @staticmethod
    def collate(list_of_dicts):
        dict_of_lists = {k: [dic[k] for dic in list_of_dicts] for k in list_of_dicts[0]}
        batch = {}
        for k, v in dict_of_lists.items():
            if isinstance(v, list):
                if all(isinstance(s, AudioSignal) for s in v):
                    batch[k] = AudioSignal.batch(v, pad_signals=True)
                    batch[k].loudness()
                else:
                    # Borrow the default collate fn from torch.
                    batch[k] = torch.utils.data._utils.collate.default_collate(v)
        return batch


class CSVDataset(BaseDataset):
    def __init__(
        self,
        sample_rate: int,
        n_examples: int = 1000,
        duration: float = 0.5,
        csv_files: List[str] = None,
        loudness_cutoff: float = -40,
        mono: bool = True,
        transform=None,
    ):
        super().__init__(
            n_examples, duration=duration, transform=transform, sample_rate=sample_rate
        )

        self.audio_lists = util.read_csv(csv_files)
        self.loudness_cutoff = loudness_cutoff
        self.mono = mono

    def __getitem__(self, idx):
        state = util.random_state(idx)

        # Load an audio file randomly from the list of lists,
        # seeded by the current index.
        audio_info = util.choose_from_list_of_lists(state, self.audio_lists)
        signal = AudioSignal.salient_excerpt(
            audio_info["path"],
            duration=self.duration,
            state=state,
            loudness_cutoff=self.loudness_cutoff,
        )
        if "loudness" in audio_info:
            signal.metadata["file_loudness"] = float(audio_info["loudness"])
        if self.mono:
            signal = signal.to_mono()
        signal = signal.resample(self.sample_rate)

        # Instantiate the transform.
        item = {"signal": signal}
        if self.transform is not None:
            item.update(self.transform.instantiate(state, signal=signal))

        return item


# Samplers
class ResumableDistributedSampler(DistributedSampler):  # pragma: no cover
    def __init__(self, dataset, start_idx=None, **kwargs):
        super().__init__(dataset, **kwargs)
        # Start index, allows to resume an experiment at the index it was
        self.start_idx = start_idx // self.num_replicas if start_idx is not None else 0

    def __iter__(self):
        for i, idx in enumerate(super().__iter__()):
            if i >= self.start_idx:
                yield idx
        self.start_idx = 0  # set the index back to 0 so for the next epoch


class ResumableSequentialSampler(SequentialSampler):  # pragma: no cover
    def __init__(self, dataset, start_idx=None, **kwargs):
        super().__init__(dataset, **kwargs)
        # Start index, allows to resume an experiment at the index it was
        self.start_idx = start_idx if start_idx is not None else 0

    def __iter__(self):
        for i, idx in enumerate(super().__iter__()):
            if i >= self.start_idx:
                yield idx
        self.start_idx = 0  # set the index back to 0 so for the next epoch
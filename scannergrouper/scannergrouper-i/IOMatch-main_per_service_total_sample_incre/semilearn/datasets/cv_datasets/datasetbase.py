import copy
import numpy as np
from PIL import Image
import torchvision
from torchvision import transforms
from torch.utils.data import Dataset

from semilearn.datasets.augmentation import RandAugment
from semilearn.datasets.utils import get_onehot


def pil_loader(path):
    # open path as file to avoid ResourceWarning (https://github.com/python-pillow/Pillow/issues/835)
    with open(path, 'rb') as f:
        img = Image.open(f)
        return img.convert('RGB')


class BasicDataset(Dataset):
    """
    BasicDataset returns a pair of image and labels (targets).
    If targets are not given, BasicDataset returns None as the label.
    This class supports strong augmentation for Fixmatch,
    and return both weakly and strongly augmented images.
    """

    def __init__(self,
                 alg,
                 data,
                 targets=None,
                 num_classes=None,
                 transform=None,
                 is_ulb=False,
                 strong_transform=None,
                 onehot=False,
                 *args,
                 **kwargs):
        """
        Args
            data: x_data
            targets: y_data (if not exist, None)
            num_classes: number of label classes
            transform: basic transformation of data
            use_strong_transform: If True, this dataset returns both weakly and strongly augmented images.
            strong_transform: list of transformation functions for strong augmentation
            onehot: If True, label is converted into onehot vector.
        """
        super(BasicDataset, self).__init__()
        self.alg = alg
        self.data = data
        print('dataset_data',data.shape)
        self.targets = targets

        self.num_classes = num_classes
        self.is_ulb = is_ulb
        self.onehot = onehot

        self.transform = transform
        self.strong_transform = strong_transform
        if self.strong_transform is None:
            if self.is_ulb:
                assert self.alg not in ['fullysupervised', 'supervised', 'pseudolabel', 'vat', 'pimodel', 'meanteacher',
                                        'mixmatch'], f"alg {self.alg} requires strong augmentation"

    def __sample__(self, idx):
        """ dataset specific sample function """
        # set idx-th target
        if self.targets is None:
            target = None
        else:
            target_ = self.targets[idx]
            target = target_ if not self.onehot else get_onehot(self.num_classes, target_)

        # set augmented images
        img = self.data[idx]
        return img, target

    def __getitem__(self, idx):
        """
        If strong augmentation is not used,
            return weak_augment_image, target
        else:
            return weak_augment_image, strong_augment_image, target
        """
        img, target = self.__sample__(idx)
        #print('img',img.shape)
        if self.transform is None:
            #print('transforms.ToTensor()(img)',transforms.ToTensor()(img))
            if self.is_ulb:
                return {'x_ulb_w': transforms.ToTensor()(img).float(),'x_ulb_s': transforms.ToTensor()(img).float(),  'y_ulb': target}
            else:
                return {'x_lb': transforms.ToTensor()(img).float(), 'y_lb': target}
        
        
        else:
            if isinstance(img, np.ndarray):
                #print('img2',img.shape)
                img = Image.fromarray(np.uint8(img))  # shape of img should be [H, W, C]
            if isinstance(img, str):
                img = pil_loader(img)

            img_w = self.transform(img)
            if not self.is_ulb:
                if self.alg in ['openmatch']:
                    return {'idx_lb': idx, 'x_lb': img_w, 'x_lb_w_0': img_w, 'x_lb_w_1': self.transform(img),
                            'y_lb': target}
                else:
                    return {'idx_lb': idx, 'x_lb': img_w, 'y_lb': target}
            else:
                if self.alg == 'fullysupervised' or self.alg == 'supervised':
                    return {'idx_ulb': idx}
                elif self.alg == 'pseudolabel' or self.alg == 'vat':
                    return {'idx_ulb': idx, 'x_ulb_w': img_w}
                elif self.alg == 'mixmatch':
                    # NOTE x_ulb_s here is weak augmentation
                    return {'idx_ulb': idx, 'x_ulb_w': img_w, 'x_ulb_s': self.transform(img), 'y_ulb': target}
                elif self.alg == 'remixmatch':
                    rotate_v_list = [0, 90, 180, 270]
                    rotate_v1 = np.random.choice(rotate_v_list, 1).item()
                    img_s1 = self.strong_transform(img)
                    img_s1_rot = torchvision.transforms.functional.rotate(img_s1, rotate_v1)
                    img_s2 = self.strong_transform(img)
                    return {'idx_ulb': idx, 'x_ulb_w': img_w, 'x_ulb_s_0': img_s1, 'x_ulb_s_1': img_s2,
                            'x_ulb_s_0_rot': img_s1_rot, 'rot_v': rotate_v_list.index(rotate_v1)}
                elif self.alg == 'comatch':
                    return {'idx_ulb': idx, 'x_ulb_w': img_w,
                            'x_ulb_s_0': self.strong_transform(img), 'x_ulb_s_1': self.strong_transform(img)}
                elif self.alg in ['mtc', 'openmatch']:
                    return {'idx_ulb': idx, 'x_ulb_w_0': img_w, 'x_ulb_w_1': self.transform(img), 'y_ulb': target}
                elif self.alg == 'openmatch_select':
                    return {'x_ulb_w': img_w, 'x_ulb_s': self.strong_transform(img)}
                else:
                    # y_ulb should be only used for evaluating pseudo-labels and be never used for training
                    return {'idx_ulb': idx, 'x_ulb_w': img_w, 'x_ulb_s': self.strong_transform(img), 'y_ulb': target}

    def __len__(self):
        return len(self.data)

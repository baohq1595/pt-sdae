import click
import numpy as np
import seaborn as sns
from sklearn.metrics import confusion_matrix
from torch.optim import SGD
from torch.optim.lr_scheduler import StepLR
import torch
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
from torchvision.datasets import MNIST
from tensorboardX import SummaryWriter
from sklearn.cluster import KMeans
import uuid

from ptsdae.sdae import StackedDenoisingAutoEncoder
import ptsdae.model as ae
from ptsdae.utils import cluster_accuracy


class CachedMNIST(Dataset):
    def __init__(self, train, cuda, testing_mode=False):
        img_transform = transforms.Compose([
            transforms.Lambda(self._transformation)
        ])
        self.ds = MNIST(
            './data',
            download=True,
            train=train,
            transform=img_transform
        )
        self.cuda = cuda
        self.testing_mode = testing_mode
        self._cache = dict()

    @staticmethod
    def _transformation(img):
        return torch.ByteTensor(torch.ByteStorage.from_buffer(img.tobytes())).float() * 0.02

    def __getitem__(self, index: int) -> torch.Tensor:
        if index not in self._cache:
            self._cache[index] = list(self.ds[index])
            if self.cuda:
                self._cache[index][0] = self._cache[index][0].cuda(non_blocking=True)
                self._cache[index][1] = torch.tensor(self._cache[index][1]).cuda(non_blocking=True)
        return self._cache[index]

    def __len__(self) -> int:
        return 128 if self.testing_mode else len(self.ds)


@click.command()
@click.option(
    '--cuda',
    help='whether to use CUDA (default False).',
    type=bool,
    default=False
)
@click.option(
    '--batch-size',
    help='training batch size (default 256).',
    type=int,
    default=256
)
@click.option(
    '--pretrain-epochs',
    help='number of pretraining epochs (default 300).',
    type=int,
    default=300
)
@click.option(
    '--finetune-epochs',
    help='number of finetune epochs (default 500).',
    type=int,
    default=500
)
@click.option(
    '--testing-mode',
    help='whether to run in testing mode (default False).',
    type=bool,
    default=False
)
def main(
    cuda,
    batch_size,
    pretrain_epochs,
    finetune_epochs,
    testing_mode
):
    writer = SummaryWriter()  # create the TensorBoard object
    # callback function to call during training, uses writer from the scope

    def training_callback(epoch, lr, loss, validation_loss):
        writer.add_scalars('data/autoencoder', {
            'lr': lr,
            'loss': loss,
            'validation_loss': validation_loss,
        }, epoch)
    ds_train = CachedMNIST(train=True, cuda=cuda, testing_mode=testing_mode)  # training dataset
    ds_val = CachedMNIST(train=False, cuda=cuda, testing_mode=testing_mode)  # evaluation dataset
    autoencoder = StackedDenoisingAutoEncoder(
        [28 * 28, 500, 500, 2000, 10],
        final_activation=None
    )
    if cuda:
        autoencoder.cuda()
    print('Pretraining stage.')
    ae.pretrain(
        ds_train,
        autoencoder,
        cuda=cuda,
        validation=ds_val,
        epochs=pretrain_epochs,
        batch_size=batch_size,
        optimizer=lambda model: SGD(model.parameters(), lr=0.1, momentum=0.9),
        scheduler=lambda x: StepLR(x, 100, gamma=0.1),
        corruption=0.2
    )
    print('Training stage.')
    ae_optimizer = SGD(params=autoencoder.parameters(), lr=0.1, momentum=0.9)
    ae.train(
        ds_train,
        autoencoder,
        cuda=cuda,
        validation=ds_val,
        epochs=finetune_epochs,
        batch_size=batch_size,
        optimizer=ae_optimizer,
        scheduler=StepLR(ae_optimizer, 100, gamma=0.1),
        corruption=0.2,
        update_callback=training_callback
    )
    print('k-Means stage')
    dataloader = DataLoader(
        ds_train,
        batch_size=1024,
        shuffle=False
    )
    kmeans = KMeans(n_clusters=10, n_init=20)
    autoencoder.eval()
    features = []
    actual = []
    for index, batch in enumerate(dataloader):
        if (isinstance(batch, tuple) or isinstance(batch, list)) and len(batch) == 2:
            batch, value = batch  # if we have a prediction label, separate it to actual
            actual.append(value)
        if cuda:
            batch = batch.cuda(non_blocking=True)
        features.append(autoencoder.encoder(batch).detach().cpu())
    actual = torch.cat(actual).long().cpu().numpy()
    predicted = kmeans.fit_predict(torch.cat(features).numpy())
    reassignment, accuracy = cluster_accuracy(actual, predicted)
    print('Final k-Means accuracy: %s' % accuracy)
    predicted_reassigned = [reassignment[item] for item in predicted]  # TODO numpify
    if not testing_mode:
        confusion = confusion_matrix(actual, predicted_reassigned)
        normalised_confusion = confusion.astype('float') / confusion.sum(axis=1)[:, np.newaxis]
        confusion_id = uuid.uuid4().hex
        sns.heatmap(normalised_confusion).get_figure().savefig('confusion_%s.png' % confusion_id)
        print('Writing out confusion diagram with UUID: %s' % confusion_id)
        writer.add_embedding(
            torch.cat(features),
            metadata=predicted,
            label_img=ds_train.ds.train_data.float().unsqueeze(1),  # TODO bit ugly
            tag='predicted'
        )
        writer.close()


if __name__ == '__main__':
    main()

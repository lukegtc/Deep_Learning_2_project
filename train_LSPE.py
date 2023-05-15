import torch
import torch.nn as nn
import torch.optim as op
from torch_geometric.datasets import ZINC
from torch_geometric.data import DataLoader
import pytorch_lightning as pl
import scipy.sparse as sp

from transform import AddRandomWalkPE
from model import LSPE_MPGNN, LSPE_MPGNNHead, LapEigLoss
from config import parse_train_args


class ZINCModel(nn.Module):
    """
    We combine here the argument preprocessing, GNN and the head.
    We should define a separate model for each dataset, because the attribute names need not be consistent between datasets.
    We unpack the attributes and make all necessary calls like .float() in the dedicated function extract_gnn_args.
    """
    def __init__(self, gnn_params, head_params):
        super().__init__()
        self.gnn = LSPE_MPGNN(**gnn_params)
        self.head = LSPE_MPGNNHead(**head_params)

    def extract_gnn_args(self, graph):
        h, edge_index, e, batch, p = graph.x, graph.edge_index, graph.edge_attr, graph.batch, graph.random_walk_pe
        h = h.float()
        e = e.unsqueeze(1).float()
        return h, e, p, edge_index, batch

    def forward(self, graph):
        h, e, p, edge_index, batch = self.extract_gnn_args(graph)
        h, p = self.gnn(h, e, p, edge_index, batch)
        out = self.head(h, p, batch)
        return out, p


class LitZINCModel(pl.LightningModule):
    def __init__(self, gnn_params, head_params, training_params):
        super().__init__()
        self.save_hyperparameters()
        self.model = ZINCModel(gnn_params, head_params)
        self.task_loss = nn.L1Loss(reduce='sum')
        self.pos_enc_loss = LapEigLoss(frobenius_norm_coeff=1e-1, pos_enc_dim=gnn_params['pos_in'])
        self.loss_alpha = 1
        self.training_params = training_params

    def training_step(self, batch, batch_idx):
        label = batch.y
        out, p = self.model(batch)
        task_loss = self.task_loss(out, label)

        normalized_laplacians = batch.normalized_lap
        lap = sp.block_diag(normalized_laplacians)
        lap = torch.from_numpy(lap.todense()).float()
        lap_eig_loss = self.pos_enc_loss(p, lap, batch.batch)

        loss = task_loss + self.loss_alpha * lap_eig_loss
        self.log("train_loss", loss)
        self.log('lr', self.trainer.optimizers[0].param_groups[0]['lr'])
        return loss

    def validation_step(self, batch, batch_idx):
        label = batch.y
        out, _ = self.model(batch)
        loss = self.task_loss(out, label)
        self.log("val_loss", loss)
        return loss

    def on_validation_epoch_end(self) -> None:
        if self.trainer.sanity_checking:
            return

        # get last train epoch loss
        train_loss = self.trainer.callback_metrics['train_loss']
        print(f'\nCurrent train loss {train_loss}')
        # get last validation epoch loss
        val_loss = self.trainer.callback_metrics['val_loss']
        print(f'Current val loss {val_loss}')

    def configure_optimizers(self):
        optimizer = op.Adam(model.parameters(), lr=self.training_params['lr'])
        scheduler = op.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=self.training_params['lr_decay'],
                                                      patience=self.training_params['patience'],
                                                      min_lr=self.training_params['min_lr'])
        return {'optimizer': optimizer, 'lr_scheduler': scheduler, 'monitor': 'val_loss'}


if __name__ == '__main__':
    args = parse_train_args()

    transform = AddRandomWalkPE(walk_length=args.walk_length)
    data_train = ZINC('datasets/ZINC', split='train', pre_transform=transform)  # QM9('datasets/QM9', pre_transform=transform)
    data_val = ZINC('datasets/ZINC', split='val', pre_transform=transform)  # QM9('datasets/QM9', pre_transform=transform)

    train_loader = DataLoader(data_train[:10000], batch_size=32)
    val_loader = DataLoader(data_val[:1000], batch_size=32)

    gnn_params = {
        'feat_in': args.feat_in,
        'pos_in': args.walk_length,
        'edge_feat_in': 1,
        'num_hidden': 32,
        'num_layers': 16
    }

    head_params = {
        'num_hidden': 32,
    }

    training_params = {
        'lr': 1e-3,
        'lr_decay': 0.5,
        'patience': 25,
        'min_lr': 1e-6,
    }

    model = LitZINCModel(gnn_params, head_params, training_params)

    trainer = pl.Trainer(max_epochs=args.max_epochs,
                         accelerator=args.accelerator,
                         devices=args.devices,
                         log_every_n_steps=10,
                         default_root_dir=args.trainer_root_dir)
    trainer.fit(model, train_loader, val_loader, ckpt_path=args.ckpt_path)

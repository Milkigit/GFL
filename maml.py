from collections import OrderedDict

import numpy as np
import torch
import torch.nn.functional as F
from torch.autograd import Variable

from utils import F1


def replace_grad(parameter_gradients, parameter_name):
    def replace_grad_(module):
        return parameter_gradients[parameter_name]

    return replace_grad_


def meta_gradient_step(args, model, optimiser, proto_model, structure_model, input_data, inner_train_steps, inner_lr,
                       train, batch_n,
                       device):

    create_graph = True and train
    meta_adj, meta_unnorm_adj, meta_features, meta_labels, meta_idx_train, meta_idx_eval, meta_weight_matrix = input_data
    task_losses = []
    task_acc = []
    task_ae_losses = []

    # Hence when we iterate over the first dimension we are iterating through the meta batches
    for meta_batch in range(batch_n):
        # adj is for the normed adjacent matrix, idx train, eval is for the train nodes, and eval nodes, weight_matrix is the adjacent matrix for each class (to calculate prototype)
        adj, unnorm_adj, features, labels, idx_train, idx_eval, weight_matrix = meta_adj[meta_batch].to(
            device), meta_unnorm_adj[meta_batch].to(device), meta_features[meta_batch].to(device), meta_labels[
                                                                                    meta_batch].to(
            device), \
                                                                                meta_idx_train[
                                                                                    meta_batch], \
                                                                                meta_idx_eval[
                                                                                    meta_batch], \
                                                                                meta_weight_matrix[
                                                                                    meta_batch].to(
                                                                                    device)
        unnorm_adj, features, adj, labels, weight_matrix = Variable(unnorm_adj), Variable(features), Variable(
            adj), Variable(labels), Variable(
            weight_matrix)

        unnorm_adj = unnorm_adj.type(torch.FloatTensor).cuda()

        idx_train_concate = []
        for idx_class in range(args.nclasses):
            idx_train_concate += idx_train[idx_class]

        fast_weights = OrderedDict(model.named_parameters())

        # Train the model for `inner_train_steps` iterations
        prototype = []
        output_repr = model.functional_forward(features, adj, fast_weights, eval=True)
        task_repr = torch.mean(output_repr, dim=0, keepdim=True)
        ae_loss = structure_model(output_repr, adj, unnorm_adj)
        community_task_repr = structure_model.forward_community(output_repr, adj)
        if args.hop_concat_type == 'fc':
            task_repr = torch.cat([task_repr, community_task_repr], dim=1)
        elif args.hop_concat_type in ['attention', 'mean']:
            task_repr = torch.cat([task_repr, community_task_repr], dim=0)
        task_repr = structure_model.forward_concat(task_repr)

        for class_graph_id in range(args.nclasses):
            class_graph_adj = weight_matrix[class_graph_id]
            class_graph_feature = output_repr[idx_train[class_graph_id]]
            prototype.append(proto_model(class_graph_feature, class_graph_adj, task_repr))

        prototype = torch.stack(prototype, dim=0)

        output_eval_emb = output_repr[idx_eval]

        output_eval = []

        for class_id in range(args.nclasses):
            output_eval.append(torch.tanh(torch.mm(F.normalize(output_eval_emb, p=2, dim=1),
                                                   F.normalize(torch.unsqueeze(prototype[class_id], dim=1), p=2,
                                                               dim=0))))
        output_eval = torch.squeeze(torch.stack(output_eval).transpose(0, 1))

        output_eval = F.log_softmax(output_eval, dim=1)

        # Do a pass of the model on the validation data from the current task

        loss = F.nll_loss(output_eval, labels[idx_eval])
        acc = F1(output_eval, labels[idx_eval])
        task_acc.append(acc)

        task_losses.append(loss)
        task_ae_losses.append(ae_loss)

    optimiser.zero_grad()

    meta_batch_loss = torch.stack(task_losses).mean() + torch.stack(task_ae_losses).mean()
    meta_batch_acc = torch.stack(task_acc).mean()
    meta_batch_ci = 1.96 * torch.stack(task_acc).std() / np.sqrt(batch_n)

    if train:
        meta_batch_loss.backward()
        optimiser.step()

    return meta_batch_loss, meta_batch_acc, meta_batch_ci

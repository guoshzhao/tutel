# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import TYPE_CHECKING, Any, Optional, Tuple, Union, cast

import os
import re
import time
import torch
import logging 
from torch import Tensor
import torch.distributed as dist

from .jit_compiler import tutel_custom_kernel

def get_world_size(group):
    try:
        return dist.get_world_size(group)
    except:
        return 1

def get_world_rank(group):
    try:
        return dist.get_rank(group)
    except:
        return 0


class AllToAll(torch.autograd.Function):
    @staticmethod
    def forward(ctx: Any, group: dist.ProcessGroup, input: Tensor):
        if not hasattr(AllToAll, '__prepared__'):
            AllToAll.__prepared__ = True
            if not hasattr(dist, 'all_to_all_single') and (AllToAll.a2a_type & 1) == 1:
                AllToAll.a2a_type ^= 3
            if (AllToAll.a2a_type & 2) == 2 and get_world_size(group) > 1:
                host_unique_id = torch.zeros([256], dtype=torch.int32).cpu()
                if get_world_rank(group) == 0:
                    tutel_custom_kernel.external_all2all(host_unique_id, 0)
                host_unique_id = host_unique_id.to(input.device)
                dist.broadcast(host_unique_id, 0, group, async_op=True).wait()
                tutel_custom_kernel.external_all2all(host_unique_id.cpu(), 1)

        ctx.group = group
        ctx.world_size = get_world_size(group)
        if ctx.world_size <= 1 or AllToAll.a2a_type == 0:
            return input
        input = input.contiguous()
        if (AllToAll.a2a_type & 8) == 8:
            torch.cuda.synchronize(input.device)
            t_start = time.time()
        if (AllToAll.a2a_type & 1) == 1:
          output = torch.empty_like(input)
          dist.all_to_all_single(output, input, group=group)
        else:
          output = tutel_custom_kernel.external_all2all(input, -1)
        if (AllToAll.a2a_type & 8) == 8:
            torch.cuda.synchronize(input.device)
            t_stop = time.time()
            if get_world_rank(group) == 0:
                logging.info('AllToAll on message size (%d x %s) costs %g sec.' % (torch.numel(input), input.dtype, t_stop - t_start))
        return output

    @staticmethod
    def backward(ctx: Any, grad_output: Tensor):
        return (None, AllToAll.apply(ctx.group, grad_output))


class PreAllreduceSum(torch.autograd.Function):
    @staticmethod
    def forward(ctx, group, input):
        ctx.group = group
        ctx.num_nodes = get_world_size(ctx.group)
        if ctx.num_nodes <= 1:
            return input
        ctx.input_shape = input.shape
        output = torch.empty([ctx.num_nodes, input.numel()], device=input.device, dtype=input.dtype)
        tensor_list = [x.contiguous() for x in torch.chunk(output, chunks=ctx.num_nodes, dim=0)]
        dist.all_gather(tensor_list=tensor_list, tensor=input.contiguous())
        output = output.view(list(input.shape[:0]) + [input.shape[0] * ctx.num_nodes] + list(input.shape[1:]))
        return output
    @staticmethod
    def backward(ctx, doutput):
        if get_world_size(ctx.group) <= 1:
            return (None, doutput)
        dinput = torch.empty(ctx.input_shape, device=doutput.device, dtype=doutput.dtype)
        chunks = [x.contiguous() for x in torch.chunk(doutput.view(ctx.num_nodes, -1), chunks=ctx.num_nodes, dim=0)]
        dist.reduce_scatter(output=dinput, input_list=chunks)
        return (None, dinput)

class PostAllreduceSum(torch.autograd.Function):
    @staticmethod
    def forward(ctx, group, input):
        ctx.group = group
        ctx.num_nodes = get_world_size(ctx.group)
        if ctx.num_nodes <= 1:
            return input
        ctx.input_shape = input.shape
        ctx.leading_dim = 0
        chunks = [x.contiguous() for x in torch.chunk(input, chunks=ctx.num_nodes, dim=ctx.leading_dim)]
        assert len(chunks) == ctx.num_nodes
        output = torch.empty_like(chunks[0])
        dist.reduce_scatter(output=output, input_list=list(chunks))
        return output
    @staticmethod
    def backward(ctx, doutput):
        if ctx.num_nodes <= 1:
            return (None, doutput)
        dinput = torch.empty(ctx.input_shape, device=doutput.device, dtype=doutput.dtype)
        tensor_list = [x.contiguous() for x in torch.chunk(dinput, chunks=ctx.num_nodes, dim=ctx.leading_dim)]
        dist.all_gather(tensor_list=tensor_list, tensor=doutput)
        return (None, dinput)


# A2A_TYPE: 0 for skip AllToAll, 1 for standard Pytorch AllToAll, 9 for standard Pytorch AllToAll with Timing
AllToAll.a2a_type = int(os.environ.get('A2A_TYPE', '1'))

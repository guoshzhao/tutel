# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import TYPE_CHECKING, Any, Optional, Tuple, Union, cast

import torch
from torch import Tensor

from .jit_compiler import IS_HIP_EXTENSION
from ..jit_kernels import sparse as jit_kernel

class GatingEncoder(torch.autograd.Function):
    @staticmethod
    def forward(ctx: Any, config: Any, reshaped_input: Tensor):
        ctx.reshaped_input = reshaped_input
        ctx.config = config

        dispatched_input = torch.zeros([ctx.config.num_global_experts * ctx.config.capacity, ctx.config.model_dim], dtype=reshaped_input.dtype, device=reshaped_input.device)
        for i in range(len(ctx.config.indices_)):
          ctx.config.func_fwd(ctx.config.ones_helper, ctx.config.indices_[i], ctx.config.locations_[i], reshaped_input, dispatched_input)
        return dispatched_input

    @staticmethod
    def backward(ctx: Any, dispatched_input: Tensor):
        dispatched_input = dispatched_input.contiguous()
        last_result = None
        for i in range(len(ctx.config.indices_)):
          grad_data = torch.empty(ctx.reshaped_input.shape, dtype=dispatched_input.dtype, device=dispatched_input.device)
          ctx.config.func_bwd_data(ctx.config.ones_helper, dispatched_input, ctx.config.indices_[i], ctx.config.locations_[i], grad_data)
          last_result = grad_data if last_result is None else last_result + grad_data
        return (None, last_result)


class GatingDecoder(torch.autograd.Function):
    @staticmethod
    def forward(ctx: Any, config: Any, expert_output: Tensor, *gates_: Tensor):
        ctx.expert_output = expert_output
        ctx.gates_h2 = [x.view(-1, 1).repeat(1, 2) if x.dtype == torch.float16 else x for x in gates_]
        ctx.config = config

        last_result = None
        for i in range(len(config.indices_)):
          single_output = torch.empty([config.expected_sample_size, config.model_dim], dtype=expert_output.dtype, device=expert_output.device)
          config.func_bwd_data(ctx.gates_h2[i], expert_output, config.indices_[i], config.locations_[i], single_output)
          last_result = single_output if last_result is None else last_result + single_output
        return last_result

    @staticmethod
    def backward(ctx: Any, combined_output: Tensor):
        combined_output = combined_output.contiguous()
        grad_expert_output = torch.zeros(ctx.expert_output.shape, dtype=combined_output.dtype, device=combined_output.device)
        for i in range(len(ctx.config.indices_)):
          ctx.config.func_fwd(ctx.gates_h2[i], ctx.config.indices_[i], ctx.config.locations_[i], combined_output, grad_expert_output)

        grad_gates = []
        for i in range(len(ctx.config.indices_)):
          grad_gates1_s = torch.empty([ctx.config.expected_sample_size,], dtype=combined_output.dtype, device=combined_output.device)
          ctx.config.func_bwd_gate(ctx.expert_output, ctx.config.indices_[i], ctx.config.locations_[i], combined_output, grad_gates1_s)
          grad_gates.append(grad_gates1_s)
        return (None, grad_expert_output, *grad_gates)


class TutelMoeFastDispatcher:

    def __init__(self, num_global_experts, capacity, model_dim, dispatch_dtype):
        self.expected_sample_size = -1
        self.num_global_experts = num_global_experts
        self.capacity = capacity
        self.model_dim = model_dim
        self.kernel_pool = dict()
        self.dtype = dispatch_dtype
        if IS_HIP_EXTENSION or dispatch_dtype != torch.float16:
            self.dtype = torch.float32
        self.original_dtype = dispatch_dtype
        self.aligned_dim = model_dim // (2 if self.dtype == torch.float16 else 1)

    def update(self, indices_, locations_, gates_, capacity=None):
        self.indices_ = [x.to(torch.int32).view(-1) for x in indices_]
        self.locations_ = [x.to(torch.int32) for x in locations_]
        self.gates_ = [x.to(self.dtype) for x in gates_]
        sample_size = self.indices_[0].size(0)
        capacity = capacity or self.capacity

        if sample_size != self.expected_sample_size or capacity != self.capacity:
            self.expected_sample_size, self.capacity = sample_size, capacity
            if tuple((sample_size, capacity)) not in self.kernel_pool:
                self.func_fwd = jit_kernel.create_forward(sample_size, self.num_global_experts, self.capacity, self.aligned_dim, self.dtype)
                self.func_bwd_data = jit_kernel.create_backward_data(sample_size, self.num_global_experts, self.capacity, self.aligned_dim, self.dtype)
                self.func_bwd_gate = jit_kernel.create_backward_gate(sample_size, self.num_global_experts, self.capacity, self.aligned_dim, self.dtype)
                self.ones_helper = torch.ones([sample_size, 2], dtype=self.dtype, device=self.indices_[0].device)
                self.kernel_pool[tuple((sample_size, capacity))] = self.func_fwd, self.func_bwd_data, self.func_bwd_gate, self.ones_helper
            else:
                self.func_fwd, self.func_bwd_data, self.func_bwd_gate, self.ones_helper = self.kernel_pool[tuple((sample_size, capacity))]

    def encode(self, data):
        return GatingEncoder.apply(self, data.to(self.dtype)).to(self.original_dtype)

    def decode(self, data):
        return GatingDecoder.apply(self, data.to(self.dtype), *self.gates_).to(self.original_dtype)

fast_dispatcher = TutelMoeFastDispatcher

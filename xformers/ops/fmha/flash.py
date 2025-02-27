# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


from dataclasses import replace
from typing import Any, Optional, Set, Tuple

import torch

from ..common import register_operator
from .common import (
    AttentionBwOpBase,
    AttentionFwOpBase,
    Context,
    Gradients,
    Inputs,
    LowerTriangularMask,
)

try:
    from ... import _C_flashattention  # type: ignore[attr-defined]

    _C_flashattention_fwd = _C_flashattention.fwd
    _C_flashattention_bwd = _C_flashattention.bwd
except ImportError:
    _C_flashattention_fwd = None
    _C_flashattention_bwd = None


def _convert_input_format(
    inp: Inputs,
) -> Tuple[Inputs, float, torch.Tensor, int, torch.Tensor, int]:
    query, key, value = inp.query, inp.key, inp.value
    batch = query.shape[0]
    seqlen_q = query.shape[1]
    seqlen_kv = key.shape[1]
    num_heads = query.shape[2]
    head_dim_q = query.shape[3]
    head_dim_v = value.shape[3]

    cu_seqlens_k = torch.arange(
        0,
        (batch + 1) * seqlen_kv,
        step=seqlen_kv,
        dtype=torch.int32,
        device=query.device,
    )
    if seqlen_q == seqlen_kv:
        cu_seqlens_q = cu_seqlens_k
    else:
        cu_seqlens_q = torch.arange(
            0,
            (batch + 1) * seqlen_q,
            step=seqlen_q,
            dtype=torch.int32,
            device=query.device,
        )

    # Initially we have `query.shape = [batch, seqlen, head_dim_q]`
    # We want format `[batch * seqlen, num_heads, head_dim_q]`
    new_inp = replace(
        inp,
        query=query.reshape([batch * seqlen_q, num_heads, head_dim_q]),
        key=key.reshape([batch * seqlen_kv, num_heads, head_dim_q]),
        value=value.reshape([batch * seqlen_kv, num_heads, head_dim_v]),
    )
    softmax_scale = inp.query.shape[-1] ** (-0.5) if inp.scale is None else inp.scale
    return new_inp, softmax_scale, cu_seqlens_q, seqlen_q, cu_seqlens_k, seqlen_kv


@register_operator
class FwOp(AttentionFwOpBase):
    """Operator that computes memory-efficient attention using \
        `Flash-Attention <https://github.com/HazyResearch/flash-attention>`_ \
        implementation.


    This is a wrapper to make FlashAttention compatible with xformers's API
    Most of this code was taken from:
    https://github.com/HazyResearch/flash-attention/blob/main/flash_attn/flash_attn_interface.py
    """

    OPERATOR = _C_flashattention_fwd
    SUPPORTED_DEVICES: Set[str] = {"cuda"}
    SUPPORTED_DTYPES: Set[torch.dtype] = {torch.half, torch.bfloat16}
    SUPPORTED_MAX_K = 128
    SUPPORTED_ATTN_BIAS_TYPES: Set[Any] = {type(None), LowerTriangularMask}
    SUPPORTS_DROPOUT = True
    SUPPORTS_CUSTOM_SCALE = True
    SUPPORTS_DIFFERENT_VALUE_EMBED = False
    NAME = "flshattF"

    @classmethod
    def supports(cls, d: "Inputs") -> bool:
        if cls.OPERATOR is None:
            return False
        if not super(FwOp, cls).supports(d):
            return False
        # We know `d.device` is cuda now
        # d=128 is only supported on A100 for bw
        # d > 64 is only supported on A100 for bw
        device_capability = torch.cuda.get_device_capability(d.device)
        if (d.query.shape[-1] % 8) > 0:
            return False
        return device_capability >= (7, 5)

    @classmethod
    def apply(
        cls, inp: Inputs, needs_gradient: bool
    ) -> Tuple[torch.Tensor, Optional[Context]]:
        if inp.attn_bias is not None and not isinstance(
            inp.attn_bias, LowerTriangularMask
        ):
            raise NotImplementedError("Unsupported attn_bias type")
        causal = isinstance(inp.attn_bias, LowerTriangularMask)
        return_softmax = False
        out_shape = [
            inp.query.shape[0],
            inp.query.shape[1],
            inp.query.shape[2],
            inp.value.shape[3],
        ]
        (
            inp,
            softmax_scale,
            cu_seqlens_q,
            max_seqlen_q,
            cu_seqlens_k,
            max_seqlen_k,
        ) = _convert_input_format(inp)
        out = torch.empty(
            [inp.query.shape[0], inp.query.shape[1], inp.value.shape[2]],
            dtype=inp.query.dtype,
            device=inp.device,
        )
        rng_state = torch.cuda.get_rng_state() if inp.p != 0.0 else None
        softmax_lse, *rest = cls.OPERATOR(
            inp.query,
            inp.key,
            inp.value,
            out,
            cu_seqlens_q,
            cu_seqlens_k,
            max_seqlen_q,
            max_seqlen_k,
            inp.p,
            softmax_scale,
            False,
            causal,
            return_softmax,
            0,  # num_splits
            None,
        )
        out = out.reshape(out_shape)
        ctx = Context(out=out, lse=softmax_lse)
        if inp.p != 0.0:
            ctx.op_bw = BwOp
            ctx.rng_state = rng_state
        return (out, ctx)


@register_operator
class BwOp(AttentionBwOpBase):
    OPERATOR = _C_flashattention_bwd
    SUPPORTED_DEVICES = FwOp.SUPPORTED_DEVICES
    SUPPORTED_DTYPES = FwOp.SUPPORTED_DTYPES
    SUPPORTED_MAX_K = FwOp.SUPPORTED_MAX_K
    SUPPORTED_ATTN_BIAS_TYPES = FwOp.SUPPORTED_ATTN_BIAS_TYPES
    SUPPORTS_DROPOUT = FwOp.SUPPORTS_DROPOUT
    SUPPORTS_CUSTOM_SCALE = FwOp.SUPPORTS_CUSTOM_SCALE
    SUPPORTS_DIFFERENT_VALUE_EMBED = FwOp.SUPPORTS_DIFFERENT_VALUE_EMBED
    NAME = "flshattB"

    @classmethod
    def supports(cls, d: Inputs) -> bool:
        if not FwOp.supports(d):
            return False
        device_capability = torch.cuda.get_device_capability(d.device)
        is_sm80 = device_capability[0] == 8 and device_capability[1] == 0
        if max(d.key.shape[-1], d.query.shape[-1]) > 64 and not is_sm80:
            return False
        return True

    @classmethod
    def apply(cls, ctx: Context, inp: Inputs, grad: torch.Tensor) -> Gradients:
        dq_shape, dk_shape, dv_shape = inp.query.shape, inp.key.shape, inp.value.shape
        (
            inp,
            softmax_scale,
            cu_seqlens_q,
            max_seqlen_q,
            cu_seqlens_k,
            max_seqlen_k,
        ) = _convert_input_format(inp)
        kernel_out_shape = [
            inp.query.shape[0],
            inp.query.shape[1],
            inp.value.shape[2],
        ]

        # Create dq,dk,dv
        # If Q/K/V come from a single QKV tensor, let's put the gradient in the
        # right strides, so we can avoid a `cat`
        if (
            inp.query.shape[0] == inp.key.shape[0]
            and inp.query.shape[2] == inp.value.shape[2]
            and inp.query.storage().data_ptr() == inp.key.storage().data_ptr()
            and inp.query.storage().data_ptr() == inp.value.storage().data_ptr()
        ):
            # Create one big contiguous chunk
            # This is because q, k and v usually come from a single
            # output of a linear layer that is chunked.
            # Creating the gradients with the right layout saves us
            # a `torch.cat` call in the backward pass
            chunk = torch.empty(
                (inp.query.shape[0], 3, inp.query.shape[1], inp.query.shape[2]),
                dtype=inp.query.dtype,
                device=inp.device,
            )
            grads = Gradients(
                dq=chunk.select(1, 0),
                dk=chunk.select(1, 1),
                dv=chunk.select(1, 2),
            )
        else:
            grads = Gradients(
                dq=torch.empty_like(inp.query),
                dk=torch.empty_like(inp.key),
                dv=torch.empty_like(inp.value),
            )

        assert grad.dtype in cls.SUPPORTED_DTYPES
        cur_rng_state = None
        if inp.p != 0.0:
            assert ctx.rng_state is not None
            cur_rng_state = torch.cuda.get_rng_state()
            torch.cuda.set_rng_state(ctx.rng_state)
        cls.OPERATOR(
            grad.reshape(kernel_out_shape).contiguous(),
            inp.query,
            inp.key,
            inp.value,
            ctx.out.reshape(kernel_out_shape),
            ctx.lse,
            grads.dq,
            grads.dk,
            grads.dv,
            cu_seqlens_q,
            cu_seqlens_k,
            max_seqlen_q,
            max_seqlen_k,
            inp.p,
            softmax_scale,
            False,
            isinstance(inp.attn_bias, LowerTriangularMask),
            0,  # num_splits
            None,
        )
        if cur_rng_state is not None:
            torch.cuda.set_rng_state(cur_rng_state)
        grads.dq = grads.dq.reshape(dq_shape)
        grads.dk = grads.dk.reshape(dk_shape)
        grads.dv = grads.dv.reshape(dv_shape)
        return grads

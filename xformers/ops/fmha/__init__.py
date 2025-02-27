# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

from typing import Any, Optional, Tuple, Type, Union

import torch

from . import cutlass, flash, small_k, triton
from .common import (
    AttentionBwOpBase,
    AttentionFwOpBase,
    AttentionMask,
    AttentionOp,
    AttentionOpBase,
    AttentionOpDispatch,
    Context,
    Gradients,
    Inputs,
    LowerTriangularMask,
    bmk2bmhk,
)
from .dispatch import _dispatch_bw, _dispatch_fw

MemoryEfficientAttentionCutlassOp = (cutlass.FwOp, cutlass.BwOp)
MemoryEfficientAttentionCutlassFwdFlashBwOp = (cutlass.FwOp, flash.BwOp)
MemoryEfficientAttentionTritonFwdFlashBwOp = (triton.FwOp, flash.BwOp)
MemoryEfficientAttentionFlashAttentionOp = (flash.FwOp, flash.BwOp)
MemoryEfficientAttentionOp = (small_k.FwOp, small_k.BwOp)
TritonFlashAttentionOp = (triton.FwOp, triton.BwOp)


class _fMHA(torch.autograd.Function):
    @staticmethod
    # type: ignore
    def forward(ctx, op: AttentionOp, *args: Any) -> Any:
        inp = Inputs(*args)
        op_fw = op[0] if op is not None else None
        op_bw = op[1] if op is not None else None

        out, op_ctx = _memory_efficient_attention_forward_requires_grad(
            inp=inp, op=op_fw
        )

        # Saving attn_bias is a bit complicated, as the
        # torch part should go in `save_for_backward`
        if isinstance(inp.attn_bias, torch.Tensor):
            attn_bias_tensor = inp.attn_bias
            attn_bias_ctx = None
        else:
            attn_bias_tensor = None
            attn_bias_ctx = inp.attn_bias

        ctx.save_for_backward(
            inp.query.detach(),
            inp.key.detach(),
            inp.value.detach(),
            op_ctx.out,
            op_ctx.lse,
            op_ctx.rng_state,
            attn_bias_tensor,
        )
        if op_ctx.op_bw is not None:
            if op_bw is not None and op_bw is not op_ctx.op_bw:
                raise ValueError(
                    f"Specified op_bw={op_bw.NAME}, but forward op "
                    f"can only run with op_bw={op_ctx.op_bw.NAME}. Please set op_bw=None."
                )
            op_bw = op_ctx.op_bw
        ctx.op_fw = op_fw
        ctx.op_bw = op_bw
        ctx.p = inp.p
        ctx.scale = inp.scale
        ctx.attn_bias_ctx = attn_bias_ctx
        ctx.n_args = len(args)
        return out

    @staticmethod
    def deserialize_bias(
        attn_bias_ctx, attn_bias_tensor: Optional[torch.Tensor]
    ) -> Any:
        if attn_bias_tensor is None:
            return attn_bias_ctx
        return attn_bias_tensor

    @classmethod
    @torch.autograd.function.once_differentiable
    def backward(cls, ctx, grad):
        assert all(
            not ctx.needs_input_grad[i] for i in range(ctx.n_args) if i not in [1, 2, 3]
        ), (
            "Only gradients to Q/K/V is implemented. "
            "For instance, it's not possible to backpropagate through the attention mask"
        )

        # Re-create context
        query, key, value, out, lse, rng_state, attn_bias_tensor = ctx.saved_tensors
        inp = Inputs(
            query=query,
            key=key,
            value=value,
            attn_bias=cls.deserialize_bias(ctx.attn_bias_ctx, attn_bias_tensor),
            p=ctx.p,
            scale=ctx.scale,
        )
        op_ctx = Context(lse=lse, out=out, rng_state=rng_state)
        grads = _memory_efficient_attention_backward(
            ctx=op_ctx, inp=inp, grad=grad, op=ctx.op_bw
        )
        return (None, grads.dq, grads.dk, grads.dv) + (None,) * (ctx.n_args - 3)


def memory_efficient_attention(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attn_bias: Optional[Union[torch.Tensor, AttentionMask]] = None,
    p: float = 0.0,
    scale: Optional[float] = None,
    *,
    op: Optional[AttentionOp] = None,
) -> torch.Tensor:
    """Implements the memory-efficient attention mechanism following
    `"Self-Attention Does Not Need O(n^2) Memory" <http://arxiv.org/abs/2112.05682>`_.

    :Inputs shape:

    - Input tensors must be in format ``[B, M, H, K]``, where B is the batch size, M \
        the sequence length, H the number of heads, and K the embeding size per head

    - If inputs have dimension 3, it is assumed that the dimensions are ``[B, M, K]`` and ``H=1``

    - Inputs can be non-contiguous - we only require the last dimension's stride to be 1


    :Equivalent pytorch code:

    .. code-block:: python

        scale = 1 / query.shape[-1] ** 0.5
        query = query * scale
        attn = query @ key.transpose(-2, -1)
        if attn_bias is not None:
            attn = attn + attn_bias
        attn = attn.softmax(-1)
        attn = F.dropout(attn, p)
        return attn @ value

    :Examples:

    .. code-block:: python

        import xformers.ops as xops

        # Compute regular attention
        y = xops.memory_efficient_attention(q, k, v)

        # With a dropout of 0.2
        y = xops.memory_efficient_attention(q, k, v, p=0.2)

        # Causal attention
        y = xops.memory_efficient_attention(
            q, k, v,
            attn_bias=xops.LowerTriangularMask()
        )

    :Supported hardware:

        NVIDIA GPUs with compute capability above 6.0 (P100+), datatype ``f16``, ``bf16`` and ``f32``.

    Raises:
        NotImplementedError: if there is no operator available to compute the MHA

    :parameter query: Tensor of shape ``[B, Mq, H, K]``
    :parameter key: Tensor of shape ``[B, Mkv, H, K]``
    :parameter value: Tensor of shape ``[B, Mkv, H, Kv]``
    :parameter attn_bias: Bias to apply to the attention matrix - defaults to no masking. \
        For causal attention, use :attr:`xformers.ops.LowerTriangularMask`. \
        This can also be a :attr:`torch.Tensor` for an arbitrary mask.
    :parameter p: Dropout probability. Disabled if set to ``0.0``
    :parameter scale: The scale to query_state weights. If set to ``None``, the default \
        scale (q.shape[-1]**-0.5) will be used.
    :parameter op: The operator to use - see :attr:`xformers.ops.AttentionOpBase`. \
        If set to ``None`` (recommended), xFormers \
        will dispatch to the best available operator, depending on the inputs \
        and options.
    :return: multi-head attention Tensor with shape ``[B, Mq, H, Kv]``
    """
    return _memory_efficient_attention(
        Inputs(
            query=query, key=key, value=value, p=p, attn_bias=attn_bias, scale=scale
        ),
        op=op,
    )


def memory_efficient_attention_forward(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attn_bias: Optional[Union[torch.Tensor, AttentionMask]] = None,
    p: float = 0.0,
    scale: Optional[float] = None,
    *,
    op: Optional[Type[AttentionFwOpBase]] = None,
) -> torch.Tensor:
    """Returns a tuple (output, lse), where `lse` can be used to compute the backward pass later"""
    return _memory_efficient_attention_forward(
        Inputs(
            query=query, key=key, value=value, p=p, attn_bias=attn_bias, scale=scale
        ),
        op=op,
    )


def memory_efficient_attention_forward_requires_grad(
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attn_bias: Optional[Union[torch.Tensor, AttentionMask]] = None,
    p: float = 0.0,
    scale: Optional[float] = None,
    *,
    op: Optional[Type[AttentionFwOpBase]] = None,
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Returns a tuple (output, lse), where `lse` can be used to compute the backward pass later.
    See :attr:`xformers.ops.memory_efficient` for an explanation of the arguments
    See :attr:`xformers.ops.memory_efficient_backward` for running the backward pass
    """
    if p != 0.0:
        raise NotImplementedError(
            "dropout is not supported on the non-autograd API."
            " If you want to use dropout, please call `memory_efficient_attention` directly"
        )
    out, ctx = _memory_efficient_attention_forward_requires_grad(
        Inputs(
            query=query, key=key, value=value, p=p, attn_bias=attn_bias, scale=scale
        ),
        op=op,
    )
    return out, ctx.lse


def memory_efficient_attention_backward(
    grad: torch.Tensor,
    output: torch.Tensor,
    lse: torch.Tensor,
    query: torch.Tensor,
    key: torch.Tensor,
    value: torch.Tensor,
    attn_bias: Optional[Union[torch.Tensor, AttentionMask]] = None,
    p: float = 0.0,
    scale: Optional[float] = None,
    *,
    op: Optional[Type[AttentionBwOpBase]] = None,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Computes the gradient of the attention.
    Returns a tuple (dq, dk, dv)
    See :attr:`xformers.ops.memory_efficient` for an explanation of the arguments.
    `lse` is the tensor returned by :attr:`xformers.ops.memory_efficient_attention_forward_requires_grad`
    """
    if p != 0.0:
        raise NotImplementedError(
            "dropout is not supported on the non-autograd API."
            " If you want to use dropout, please call `memory_efficient_attention` directly"
        )
    gradients = _memory_efficient_attention_backward(
        Context(out=output, lse=lse),
        Inputs(
            query=query, key=key, value=value, p=p, attn_bias=attn_bias, scale=scale
        ),
        grad,
        op=op,
    )
    return (gradients.dq, gradients.dk, gradients.dv)


def _memory_efficient_attention(
    inp: Inputs, op: Optional[AttentionOp] = None
) -> torch.Tensor:
    # fast-path that doesn't require computing the logsumexp for backward computation
    if all(x.requires_grad is False for x in [inp.query, inp.key, inp.value]):
        return _memory_efficient_attention_forward(
            inp, op=op[0] if op is not None else None
        )

    output_shape = inp.normalize_bmhk()
    return _fMHA.apply(
        op, inp.query, inp.key, inp.value, inp.attn_bias, inp.p, inp.scale
    ).reshape(output_shape)


def _memory_efficient_attention_forward(
    inp: Inputs, op: Optional[Type[AttentionFwOpBase]]
) -> torch.Tensor:
    output_shape = inp.normalize_bmhk()
    if op is None:
        op = _dispatch_fw(inp)
    return op.apply(inp, needs_gradient=False)[0].reshape(output_shape)


def _memory_efficient_attention_forward_requires_grad(
    inp: Inputs, op: Optional[Type[AttentionFwOpBase]]
) -> Tuple[torch.Tensor, Context]:
    output_shape = inp.normalize_bmhk()
    if op is None:
        op = _dispatch_fw(inp)
    out = op.apply(inp, needs_gradient=True)
    assert out[1] is not None
    return (out[0].reshape(output_shape), out[1])


def _memory_efficient_attention_backward(
    ctx: Context, inp: Inputs, grad: torch.Tensor, op: Optional[Type[AttentionBwOpBase]]
) -> Gradients:
    """Warning: grad/ctx.out is potentially in BMK format"""
    if grad.ndim != inp.query.ndim or grad.ndim != ctx.out.ndim:
        raise ValueError(
            "All tensors should be either in BMK (ndim=3) or BMHK (ndim=4) format. \n"
            f"grad.shape : {grad.shape} \n"
            f"out.shape  : {ctx.out.shape} \n"
            f"query.shape: {inp.query.shape}"
        )
    inp.normalize_bmhk()
    # LSE has shape [B, H, M] while query has shape [B, M, H, K]
    if (
        ctx.lse.ndim != 3
        or ctx.lse.shape[0] != inp.query.shape[0]
        or ctx.lse.shape[1] != inp.query.shape[2]
        or ctx.lse.shape[2] < inp.query.shape[1]
    ):
        raise ValueError(
            "Input tensors have incompatible shapes."
            f"lse.shape    : {ctx.lse.shape} \n"
            f"query.shape  : {inp.query.shape}"
        )
    grad = bmk2bmhk(grad, 1)
    ctx.out = bmk2bmhk(ctx.out, 1)

    if op is None:
        op = _dispatch_bw(inp)
    return op.apply(ctx, inp, grad)


__all__ = [
    "AttentionMask",
    "AttentionOp",
    "AttentionOpBase",
    "AttentionOpDispatch",
    "LowerTriangularMask",
    "MemoryEfficientAttentionCutlassFwdFlashBwOp",
    "MemoryEfficientAttentionTritonFwdFlashBwOp",
    "MemoryEfficientAttentionCutlassOp",
    "MemoryEfficientAttentionFlashAttentionOp",
    "MemoryEfficientAttentionOp",
    "TritonFlashAttentionOp",
    "memory_efficient_attention",
]

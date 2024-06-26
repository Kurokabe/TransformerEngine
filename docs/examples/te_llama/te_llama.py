# Copyright (c) 2022-2024, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# See LICENSE for license information.

import os
import re
import gc
from contextlib import contextmanager

import torch
from torch import nn

import transformer_engine as te
from transformer_engine.pytorch.attention import RotaryPositionEmbedding
from transformer_engine.pytorch.fp8 import fp8_model_init

import transformers
from transformers.models.llama.modeling_llama import LlamaModel, LlamaForCausalLM, LlamaRMSNorm, LlamaConfig
from transformers.modeling_utils import _add_variant, load_state_dict, _load_state_dict_into_model
from transformers.utils import WEIGHTS_INDEX_NAME
from transformers.utils.hub import get_checkpoint_shard_files

@contextmanager
def replace_decoder(te_decodder_cls):
    """
    Replace `LlamaDecoderLayer` with custom `TELlamaDecoderLayer`.
    """
    original_llama_decoder_cls = transformers.models.llama.modeling_llama.LlamaDecoderLayer
    transformers.models.llama.modeling_llama.LlamaDecoderLayer = te_decodder_cls
    try:
        yield
    finally:
        transformers.models.llama.modeling_llama.LlamaDecoderLayer = original_llama_decoder_cls


class TELlamaDecoderLayer(te.pytorch.TransformerLayer):
    """
    Wrapper class over TE's `TransformerLayer`. This makes the wrapper very
    similar to HF's `LlamaDecoderLayer` and easier to replace it in the code.

    Args:
        config: LlamaConfig
        args: positional args (for compatibility with `LlamaDecoderLayer`)
        kwargs: keyword args (for compatibility with `LlamaDecoderLayer`)
    """
    def __init__(self, config, *args, **kwargs):
        super().__init__(
            hidden_size=config.hidden_size,
            ffn_hidden_size=config.intermediate_size,
            num_attention_heads=config.num_attention_heads,
            bias=False,
            layernorm_epsilon=config.rms_norm_eps,
            hidden_dropout=0,
            attention_dropout=0,
            fuse_qkv_params=False,
            normalization="RMSNorm",
            activation="swiglu",
            attn_input_format="bshd",
        )
        te_rope = RotaryPositionEmbedding(config.hidden_size//config.num_attention_heads)
        self.te_rope_emb = te_rope(max_seq_len=config.max_position_embeddings).cuda()

    def forward(self,
                hidden_states,
                *args,
                attention_mask,
                **kwargs):
        """
        Custom forward to make sure we only pass relevant arguments to the
        forward pass of the `TransformerLayer`. Also, make sure the output
        format matches the output of the HF's `LlamaDecoderLayer`.
        """
        return (super().forward(hidden_states, attention_mask=attention_mask, rotary_pos_emb=self.te_rope_emb),)


class TELlamaForCausalLM:
    """
    Causal LM created with `LlamaModel`. The underlying `LlamaDecoderLayer`
    class is monkey-patched with `TELlamaDecoderLayer` class before
    initializing the causal LM with `LlamaForCausalLM`.

    Args:
        config: LlamaConfig
    """

    def __new__(cls, config: LlamaConfig):
        with replace_decoder(te_decodder_cls=TELlamaDecoderLayer):
            llama_for_causal_lm = LlamaForCausalLM(config)
        return llama_for_causal_lm

    @classmethod
    def from_pretrained_local(cls, pretrained_model_name_or_path, *args, config, **kwargs):
        """
        Custom method adapted from `from_pretrained` method in HuggingFace
        Transformers repo: https://github.com/huggingface/transformers/blob/f497f564bb76697edab09184a252fc1b1a326d1e/src/transformers/modeling_utils.py#L2579
        """
        vanilla_model = cls(config).to(kwargs['torch_dtype'])
        is_local = os.path.isdir(pretrained_model_name_or_path)
        subfolder = ""
        variant = None
        if os.path.isfile(
                    os.path.join(pretrained_model_name_or_path, subfolder, _add_variant(WEIGHTS_INDEX_NAME, variant))
            ):
                # Load from a sharded PyTorch checkpoint
                archive_file = os.path.join(
                    pretrained_model_name_or_path, subfolder, _add_variant(WEIGHTS_INDEX_NAME, variant)
                )
                is_sharded = True
        else:
            raise AssertionError("Only sharded PyTorch ckpt format supported at the moment")


        resolved_archive_file, sharded_metadata = get_checkpoint_shard_files(
                pretrained_model_name_or_path,
                archive_file,
        )

        # If the checkpoint is not sharded, it's a trivial sharding case
        if not is_sharded:
            assert not isinstance(resolved_archive_file, list)
            resolved_archive_file = [resolved_archive_file]

        error_msgs = []
        for shard_file in resolved_archive_file:
            state_dict = load_state_dict(shard_file)
            replaced_layers = replace_params(state_dict, vanilla_model.state_dict())

            error_msgs += _load_state_dict_into_model(vanilla_model, state_dict, start_prefix="")

            # Force mem release. Taken from huggingface code
            del state_dict
            gc.collect()

        return vanilla_model

def replace_params(hf_state_dict, te_state_dict):
    # collect all layer prefixes to update
    all_layer_prefixes = set()
    for param_key in hf_state_dict.keys():
        layer_prefix_pat = 'model.layers.\d+.'
        m = re.match(layer_prefix_pat, param_key)
        if m is not None:
            all_layer_prefixes.add(m.group())

    for layer_prefix in all_layer_prefixes:
        # When loading weights into models with less number of layers, skip the
        # copy if the corresponding layer doesn't exist in TE model
        if layer_prefix + 'self_attention.layernorm_qkv.layer_norm_weight' in te_state_dict:
            te_state_dict[layer_prefix + 'self_attention.layernorm_qkv.layer_norm_weight'].data[:] = hf_state_dict[layer_prefix + 'input_layernorm.weight'].data[:]

        if layer_prefix + 'self_attention.layernorm_qkv.query_weight' in te_state_dict:
            te_state_dict[layer_prefix + 'self_attention.layernorm_qkv.query_weight'].data[:] = hf_state_dict[layer_prefix + 'self_attn.q_proj.weight'].data[:]

        if layer_prefix + 'self_attention.layernorm_qkv.key_weight' in te_state_dict:
            te_state_dict[layer_prefix + 'self_attention.layernorm_qkv.key_weight'].data[:] = hf_state_dict[layer_prefix + 'self_attn.k_proj.weight'].data[:]

        if layer_prefix + 'self_attention.layernorm_qkv.value_weight' in te_state_dict:
            te_state_dict[layer_prefix + 'self_attention.layernorm_qkv.value_weight'].data[:] = hf_state_dict[layer_prefix + 'self_attn.v_proj.weight'].data[:]

        if layer_prefix + 'self_attention.proj.weight' in te_state_dict:
            te_state_dict[layer_prefix + 'self_attention.proj.weight'].data[:] = hf_state_dict[layer_prefix + 'self_attn.o_proj.weight'].data[:]

        if layer_prefix + 'layernorm_mlp.layer_norm_weight' in te_state_dict:
            te_state_dict[layer_prefix + 'layernorm_mlp.layer_norm_weight'].data[:] = hf_state_dict[layer_prefix + 'post_attention_layernorm.weight'].data[:]

        if layer_prefix + 'layernorm_mlp.fc1_weight' in te_state_dict:
            te_state_dict[layer_prefix + 'layernorm_mlp.fc1_weight'].data[:] = torch.cat((hf_state_dict[layer_prefix + 'mlp.gate_proj.weight'].data[:], hf_state_dict[layer_prefix + 'mlp.up_proj.weight'].data[:]), dim=0)

        if layer_prefix + 'layernorm_mlp.fc2_weight' in te_state_dict:
            te_state_dict[layer_prefix + 'layernorm_mlp.fc2_weight'].data[:] = hf_state_dict[layer_prefix + 'mlp.down_proj.weight'].data[:]

    return all_layer_prefixes
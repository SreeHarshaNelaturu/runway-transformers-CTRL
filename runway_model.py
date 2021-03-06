#!/usr/bin/env python3
# coding=utf-8
# Copyright 2018 Google AI, Google Brain and Carnegie Mellon University Authors and the HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Conditional text generation with the auto-regressive models of the library (GPT/GPT-2/CTRL/Transformer-XL/XLNet)
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import argparse
import logging
from tqdm import trange

import torch
import torch.nn.functional as F
import numpy as np
from transformers import CTRLConfig
from transformers import CTRLLMHeadModel, CTRLTokenizer
from runway.data_types import *
import runway
from control_codes import *

MAX_LENGTH = int(10000)  # Hardcoded max length to avoid infinite loop


MODEL_CLASSES = {
    'ctrl': ( CTRLLMHeadModel, CTRLTokenizer)
}

def set_seed(seed, n_gpu):
    np.random.seed(seed)
    torch.manual_seed(seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(seed)


def top_k_top_p_filtering(logits, top_k=0, top_p=0.0, filter_value=-float('Inf')):
    """ Filter a distribution of logits using top-k and/or nucleus (top-p) filtering
        Args:
            logits: logits distribution shape (batch size x vocabulary size)
            top_k > 0: keep only top k tokens with highest probability (top-k filtering).
            top_p > 0.0: keep the top tokens with cumulative probability >= top_p (nucleus filtering).
                Nucleus filtering is described in Holtzman et al. (http://arxiv.org/abs/1904.09751)
        From: https://gist.github.com/thomwolf/1a5a29f6962089e871b94cbd09daf317
    """
    top_k = min(top_k, logits.size(-1))  # Safety check
    if top_k > 0:
        # Remove all tokens with a probability less than the last token of the top-k
        indices_to_remove = logits < torch.topk(logits, top_k)[0][..., -1, None]
        logits[indices_to_remove] = filter_value

    if top_p > 0.0:
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        cumulative_probs = torch.cumsum(F.softmax(sorted_logits, dim=-1), dim=-1)

        # Remove tokens with cumulative probability above the threshold
        sorted_indices_to_remove = cumulative_probs > top_p
        # Shift the indices to the right to keep also the first token above the threshold
        sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
        sorted_indices_to_remove[..., 0] = 0

        # scatter sorted tensors to original indexing
        indices_to_remove = sorted_indices_to_remove.scatter(dim=1, index=sorted_indices, src=sorted_indices_to_remove)
        logits[indices_to_remove] = filter_value
    return logits


def sample_sequence(model, length, context, num_samples=1, temperature=1, top_k=0, top_p=0.0, repetition_penalty=1.0, device='cpu'):
    context = torch.tensor(context, dtype=torch.long, device=device)
    context = context.unsqueeze(0).repeat(num_samples, 1)
    generated = context
    with torch.no_grad():
        for _ in trange(length):

            inputs = {'input_ids': generated}


           
            outputs = model(**inputs)  # Note: we could also use 'past' with GPT-2/Transfo-XL/XLNet/CTRL (cached hidden-states)
            next_token_logits = outputs[0][:, -1, :] / (temperature if temperature > 0 else 1.)

            # repetition penalty from CTRL (https://arxiv.org/abs/1909.05858)
            for i in range(num_samples):
                for _ in set(generated[i].tolist()):
                    next_token_logits[i, _] /= repetition_penalty
                
            filtered_logits = top_k_top_p_filtering(next_token_logits, top_k=top_k, top_p=top_p)
            if temperature == 0: # greedy sampling:
                next_token = torch.argmax(filtered_logits, dim=-1).unsqueeze(-1)
            else:
                next_token = torch.multinomial(F.softmax(filtered_logits, dim=-1), num_samples=1)
            generated = torch.cat((generated, next_token), dim=1)
    return generated


@runway.setup
def setup():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    n_gpu = torch.cuda.device_count()
    
    seed = 42
    set_seed(seed, n_gpu)

    model_type = 'ctrl'
    model_class, tokenizer_class = MODEL_CLASSES[model_type]
    tokenizer = tokenizer_class.from_pretrained('ctrl')
    model = model_class.from_pretrained('ctrl')
    model.to(device)
    model.eval()
    
    return {"model" : model,
            "tokenizer" : tokenizer, 
            "device" : device}

command_inputs = {
    "input_prompt" : text,
    "control_code" : category(default="Politics", choices=list(CONTROL_CODES.keys()), description="Control codes specify domain, subdomain, entities, relationships between entities, dates, and task-specific behavior"), 
    "length" : number(default=20, min=20, max=500, step=1, description="Output Text Length"),
    "temperature" : number(default=0.7, min=0, max=1, step=0.01,  description="The high temperature sample displays greater linguistic variety, but the low temperature sample is more grammatically correct. CTRL Works better with lower temperature. "),
    "top_p" : number(default=0.9, min=0, max = 1, step=0.01, description="The cumulative probability of token sequences to sample from. Lower values lead to higher quality but less surprising results.")
    }

command_outputs = {"generated_text" : text}                         

@runway.command("generated_text", inputs=command_inputs, outputs=command_outputs, description="Generate text conditioned on prompt")
def generate_text(model_opts, inputs):

    control_word = CONTROL_CODES[inputs["control_code"]]
    model = model_opts["model"]
    tokenizer = model_opts["tokenizer"]
    device = model_opts["device"]

    length = inputs["length"]
    num_samples = 1
    temperature = inputs["temperature"]
    repetition_penalty = 1.2
    top_k = 0
    top_p = inputs["top_p"]
    
    stop_token = None
    
    if length < 0 and model.config.max_position_embeddings > 0:
        length = model.config.max_position_embeddings
    elif 0 < model.config.max_position_embeddings < length:
        length = model.config.max_position_embeddings  # No generation bigger than model size 
    elif length < 0:
        length = MAX_LENGTH  # avoid infinite loop

    while True:
        

        raw_text = inputs["input_prompt"]
        # Models with memory likes to have a long prompt for short inputs.
        context_tokens = tokenizer.encode(raw_text, add_special_tokens=False)
        
        context_tokens.insert(0, control_word)
        
        out = sample_sequence(
            model=model,
            context=context_tokens,
            num_samples=num_samples,
            length=length,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            device=device,
        )
        out = out[:, len(context_tokens):].tolist()
        for o in out:
            text = tokenizer.decode(o, clean_up_tokenization_spaces=True)
            text = text[: text.find(stop_token) if stop_token else None]
        
        if raw_text:
           break
        
        
    return raw_text + " " + text


if __name__ == '__main__':
    runway.run()

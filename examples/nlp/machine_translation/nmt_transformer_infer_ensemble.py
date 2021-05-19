# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""
Given NMT model's .nemo file, this script can be used to translate text.
USAGE Example:
1. Obtain text file in src language. You can use sacrebleu to obtain standard test sets like so:
    sacrebleu -t wmt14 -l de-en --echo src > wmt14-de-en.src
2. Translate:
    python nmt_transformer_infer.py --model=[Path to .nemo file] --srctext=wmt14-de-en.src --tgtout=wmt14-de-en.pre
"""


from argparse import ArgumentParser

import numpy as np
import torch

import nemo.collections.nlp as nemo_nlp
from nemo.collections.nlp.modules.common.transformer.transformer_generators import EnsembleBeamSearchSequenceGenerator
from nemo.utils import logging


def filter_predicted_ids(tokenizer, ids):
    ids[ids >= tokenizer.vocab_size] = tokenizer.unk_id
    return ids


def nmt_postprocess(beam_results, model):
    beam_results = filter_predicted_ids(model.decoder_tokenizer, beam_results)
    translations = [model.decoder_tokenizer.ids_to_text(tr) for tr in beam_results.cpu().numpy()]
    if model.target_processor is not None:
        translations = [model.target_processor.detokenize(translation.split(' ')) for translation in translations]

    return translations


def input_preprocess(text, model):
    inputs = []
    for txt in text:
        if model.source_processor is not None:
            txt = model.source_processor.normalize(txt)
            txt = model.source_processor.tokenize(txt)
        ids = model.encoder_tokenizer.text_to_ids(txt)
        ids = [model.encoder_tokenizer.bos_id] + ids + [model.encoder_tokenizer.eos_id]
        inputs.append(ids)
    max_len = max(len(txt) for txt in inputs)
    src_ids_ = np.ones((len(inputs), max_len)) * model.encoder_tokenizer.pad_id
    for i, txt in enumerate(inputs):
        src_ids_[i][: len(txt)] = txt

    src_mask = torch.FloatTensor((src_ids_ != model.encoder_tokenizer.pad_id))
    src = torch.LongTensor(src_ids_)
    return src, src_mask


def main():
    parser = ArgumentParser()
    parser.add_argument("--models", type=str, required=True, help="Comma separated list of NeMo model paths")
    parser.add_argument("--srctext", type=str, required=True, help="Path to input file to be translated")
    parser.add_argument("--tgtout", type=str, required=True, help="Path to output file to write translations")
    parser.add_argument("--batch_size", type=int, default=256, help="Number of sentences to batch together")
    parser.add_argument("--beam_size", type=int, default=4, help="Beam Size")
    parser.add_argument("--len_pen", type=float, default=0.6, help="Length Penalty")
    parser.add_argument(
        "--max_delta_length", type=int, default=5, help="Maximum length difference between input and output"
    )
    parser.add_argument("--target_lang", type=str, default=None, help="Target language ID")
    parser.add_argument("--source_lang", type=str, default=None, help="Source language ID")

    args = parser.parse_args()
    torch.set_grad_enabled(False)
    models = [
        nemo_nlp.models.machine_translation.MTEncDecModel.restore_from(restore_path=model_path)
        for model_path in args.models.split(',')
    ]
    src_text = []
    tgt_text = []

    if torch.cuda.is_available():
        models = [model.cuda() for model in models]

    ensemble_generator = EnsembleBeamSearchSequenceGenerator(
        encoders=[model.encoder for model in models],
        embeddings=[model.decoder.embedding for model in models],
        decoders=[model.decoder.decoder for model in models],
        log_softmaxes=[model.log_softmax for model in models],
        max_sequence_length=512,
        beam_size=args.beam_size,
        bos=models[0].decoder_tokenizer.bos_id,
        pad=models[0].decoder_tokenizer.pad_id,
        eos=models[0].decoder_tokenizer.eos_id,
        len_pen=args.len_pen,
        max_delta_length=args.max_delta_length,
    )
    logging.info(f"Translating: {args.srctext}")

    count = 0
    with open(args.srctext, 'r') as src_f:
        for line in src_f:
            src_text.append(line.strip())
            if len(src_text) == args.batch_size:
                src_ids, src_mask = input_preprocess(src_text, models[0])
                src_ids = src_ids.to(models[0].device)
                src_mask = src_mask.to(models[0].device)
                beam_results = ensemble_generator(src_ids, src_mask)
                res = nmt_postprocess(beam_results, models[0])
                if len(res) != len(src_text):
                    print(len(res))
                    print(len(src_text))
                    print(res)
                    print(src_text)
                tgt_text += res
                src_text = []
            count += 1
            # if count % 300 == 0:
            #    print(f"Translated {count} sentences")
        if len(src_text) > 0:
            src_ids, src_mask = input_preprocess(src_text, models[0])
            src_ids = src_ids.to(models[0].device)
            src_mask = src_mask.to(models[0].device)
            beam_results = ensemble_generator(src_ids, src_mask)
            res = nmt_postprocess(beam_results, models[0])
            tgt_text += res

    with open(args.tgtout, 'w') as tgt_f:
        for line in tgt_text:
            tgt_f.write(line + "\n")


if __name__ == '__main__':
    main()  # noqa pylint: disable=no-value-for-parameter

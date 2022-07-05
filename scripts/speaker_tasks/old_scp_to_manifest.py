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

import argparse
import json
import logging
import os
import random

import librosa as l
from sklearn.model_selection import StratifiedShuffleSplit
from tqdm import tqdm

random.seed(42)

"""
This scipt converts a scp file where each line contains  
<absolute path of wav file> 
to a manifest json file. 
Args: 
--scp: scp file name
--id: index of speaker label in filename present in scp file that is separated by '/'
--out: output manifest file name
--split: True / False if you would want to split the  manifest file for training purposes
        you may not need this for test set. output file names is <out>_<train/dev>.json
        Defaults to False
--create_chunks: bool if you would want to chunk each manifest line to chunks of 3 sec or less
        you may not need this for test set, Defaults to False
"""

# MIN_DURATIONS = [1.5, 2, 3]
MIN_DURATIONS = [4, 5, 6]
# change
# MIN_DURATIONS = [8]


def filter_manifest_line(manifest_line, signal, sr=16000, to_slice=False, slice_output_folder=None, keep_shorter=False):
    split_manifest = []
    speakers = []
    audio_filepath = manifest_line['audio_filepath']
    start = manifest_line.get('offset', 0)
    dur = manifest_line['duration']
    SPKR = manifest_line['label']

    if dur >= 4: #1.5
        remaining_dur = dur
        temp_dur = random.choice(MIN_DURATIONS)
        remaining_dur = remaining_dur - temp_dur
        while remaining_dur >= 0:
            segment_audio = signal[int(start * sr) : int(start * sr + temp_dur * sr)]
            if l.feature.rms(y=segment_audio).mean() > 0.01:
                if to_slice:
                    
                    audio_filepath_name = audio_filepath.split("/")[-1].split(".wav")[0] + "_" + str(start) + "_"+ str(temp_dur) + ".wav"
                    lang_folder =  os.path.join(slice_output_folder, audio_filepath.split("/")[-2])
                    os.makedirs(lang_folder, exist_ok=True)
                    audio_filepath_seg = os.path.join(lang_folder, audio_filepath_name)

                    os.system(f"sox {audio_filepath} {audio_filepath_seg} trim {start} {temp_dur}")
                    meta = {'audio_filepath': audio_filepath_seg, 'offset': 0, 'duration': temp_dur, 'label': SPKR}
                    
                else:
                     meta = {'audio_filepath': audio_filepath, 'offset': start, 'duration': temp_dur, 'label': SPKR}
                split_manifest.append(meta)
                speakers.append(SPKR)

              
            start = start + temp_dur
            temp_dur = random.choice(MIN_DURATIONS)
            remaining_dur = remaining_dur - temp_dur
            
        if keep_shorter:
            remaining_dur =  remaining_dur + temp_dur
            if remaining_dur >= 0.5:
                meta = {'audio_filepath': audio_filepath, 'offset': start, 'duration': remaining_dur, 'label': SPKR}
                split_manifest.append(meta)
            
         
    else:
        if keep_shorter:
            print("shorter", dur)
            meta = {'audio_filepath': audio_filepath, 'offset': 0, 'duration': dur, 'label': SPKR}
            split_manifest.append(meta)
            
    return split_manifest, speakers


def write_file(name, lines, idx):
    with open(name, 'w') as fout:
        for i in idx:
            dic = lines[i]
            json.dump(dic, fout)
            fout.write('\n')
    logging.info("wrote", name)


# def main(scp, id, out, split=False, create_chunks=False):
def main(scp, out, split=False, create_chunks=False, to_slice=False, slice_output_folder=None, keep_shorter=False):
    if os.path.exists(out):
        os.remove(out)
    scp_file = open(scp, 'r').readlines()
    scp_file = sorted(scp_file)

    lines = []
    speakers = []
    for line in tqdm(scp_file):
        line = line.strip()
#         y, sr = l.load(line, sr=None)
#         dur = l.get_duration(y=y, sr=sr)
#         speaker = line.split('/')[id]
#         speaker = list(speaker)
#         speaker = ''.join(speaker)
#         meta = [{"audio_filepath": line, "offset": 0, "duration": float(dur), "label": speaker}]
#         speaker = [speaker]


    
            
        lang, audio_filepath, dur = line.split(",")
        y, sr = l.load(audio_filepath, sr=None)
        meta = [{"audio_filepath": audio_filepath, "offset": 0, "duration": float(dur), "label": lang}]
        speaker = [lang]
        
        if create_chunks:
            meta, speaker = filter_manifest_line(meta[0], signal=y, sr=sr, to_slice=to_slice, slice_output_folder=slice_output_folder, keep_shorter=keep_shorter)
        lines.extend(meta)
        speakers.extend(speaker)

    write_file(out, lines, range(len(lines)))
    
    
    

    if split:
        path = os.path.dirname(out)
        out_name = out.split(".json")[0]
        
        sss = StratifiedShuffleSplit(n_splits=1, test_size=0.1, random_state=42)
        for train_idx, test_idx in sss.split(speakers, speakers):
            logging.info(len(train_idx))

        out = os.path.join(path, out_name + '_train.json')
        write_file(out, lines, train_idx)
        out = os.path.join(path, out_name + '_dev.json')
        write_file(out, lines, test_idx)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--scp", help="scp file name", type=str, required=True)
#     parser.add_argument(
#         "--id", help="field num seperated by '/' to be considered as speaker label", type=int, required=True
#     )
    parser.add_argument("--out", help="manifest_file name", type=str, required=True)
    parser.add_argument(
        "--split",
        help="bool if you would want to split the manifest file for training purposes",
        required=False,
        action='store_true',
    )
    parser.add_argument(
        "--create_chunks",
        help="bool if you would want to chunk each manifest line to chunks of 3 sec or less",
        required=False,
        action='store_true',
    )
    parser.add_argument(
        "--to_slice",
        help="bool if you would want to slice audio",
        required=False,
        action='store_true',
    )
    parser.add_argument(
        "--keep_shorter",
        help="bool if you would want to keep shorter than duration snippet",
        required=False,
        action='store_true',
    )
    
    
    parser.add_argument("--slice_output_folder", help="manifest_file name", type=str)
    args = parser.parse_args()

#     main(args.scp, args.id, args.out, args.split, args.create_chunks)
    main(args.scp, args.out, args.split, args.create_chunks, args.to_slice, args.slice_output_folder, args.keep_shorter)

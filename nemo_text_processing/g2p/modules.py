# -*- coding: utf-8 -*-
# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
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

import pathlib
import random
import re
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

import nltk
import torch
from nemo_text_processing.g2p.data.data_utils import (
    GRAPHEME_CASE_MIXED,
    GRAPHEME_CASE_UPPER,
    LATIN_CHARS_ALL,
    any_locale_word_tokenize,
    english_word_tokenize,
    normalize_unicode_text,
    set_grapheme_case,
)

from nemo.collections.common.tokenizers.text_to_speech.ipa_lexicon import validate_locale
from nemo.utils import logging
from nemo.utils.decorators import experimental
from nemo.utils.get_rank import is_global_rank_zero


class BaseG2p(ABC):
    def __init__(
        self,
        phoneme_dict=None,
        word_tokenize_func=lambda x: x,
        apply_to_oov_word=None,
        mapping_file: Optional[str] = None,
    ):
        """Abstract class for creating an arbitrary module to convert grapheme words
        to phoneme sequences, leave unchanged, or use apply_to_oov_word.
        Args:
            phoneme_dict: Arbitrary representation of dictionary (phoneme -> grapheme) for known words.
            word_tokenize_func: Function for tokenizing text to words.
            apply_to_oov_word: Function that will be applied to out of phoneme_dict word.
        """
        self.phoneme_dict = phoneme_dict
        self.word_tokenize_func = word_tokenize_func
        self.apply_to_oov_word = apply_to_oov_word
        self.mapping_file = mapping_file
        self.heteronym_model = None  # heteronym classification model

    @abstractmethod
    def __call__(self, text: str) -> str:
        pass

    def setup_heteronym_model(
        self,
        heteronym_model,
        wordid_to_phonemes_file: str = "../../../scripts/tts_dataset_files/wordid_to_ipa-0.7b_nv22.10.tsv",
    ):
        """
        Add heteronym classification model to TTS preprocessing pipeline to disambiguate heteronyms.
            Heteronym model has a list of supported heteronyms but only heteronyms specified in
            wordid_to_phonemes_file will be converted to phoneme form during heteronym model inference;
            the rest will be left in grapheme form.

        Args:
            heteronym_model: Initialized HeteronymClassificationModel
            wordid_to_phonemes_file: Path to a file with mapping from wordid predicted by heteronym model to phonemes
        """

        try:
            from nemo_text_processing.g2p.models.heteronym_classification import HeteronymClassificationModel

            self.heteronym_model = heteronym_model
            self.heteronym_model.set_wordid_to_phonemes(wordid_to_phonemes_file)
        except ImportError as e:
            logging.warning("Heteronym model setup will be skipped")
            logging.error(e)


class EnglishG2p(BaseG2p):
    def __init__(
        self,
        phoneme_dict=None,
        word_tokenize_func=english_word_tokenize,
        apply_to_oov_word=None,
        ignore_ambiguous_words=True,
        heteronyms=None,
        encoding='latin-1',
        phoneme_probability: Optional[float] = None,
        mapping_file: Optional[str] = None,
    ):
        """English G2P module. This module converts words from grapheme to phoneme representation using phoneme_dict in CMU dict format.
        Optionally, it can ignore words which are heteronyms, ambiguous or marked as unchangeable by word_tokenize_func (see code for details).
        Ignored words are left unchanged or passed through apply_to_oov_word for handling.
        Args:
            phoneme_dict (str, Path, Dict): Path to file in CMUdict format or dictionary of CMUdict-like entries.
            word_tokenize_func: Function for tokenizing text to words.
                It has to return List[Tuple[Union[str, List[str]], bool]] where every tuple denotes word representation and flag whether to leave unchanged or not.
                It is expected that unchangeable word representation will be represented as List[str], other cases are represented as str.
                It is useful to mark word as unchangeable which is already in phoneme representation.
            apply_to_oov_word: Function that will be applied to out of phoneme_dict word.
            ignore_ambiguous_words: Whether to not handle word via phoneme_dict with ambiguous phoneme sequences. Defaults to True.
            heteronyms (str, Path, List): Path to file with heteronyms (every line is new word) or list of words.
            encoding: Encoding type.
            phoneme_probability (Optional[float]): The probability (0.<var<1.) that each word is phonemized. Defaults to None which is the same as 1.
                Note that this code path is only run if the word can be phonemized. For example: If the word does not have an entry in the g2p dict, it will be returned
                as characters. If the word has multiple entries and ignore_ambiguous_words is True, it will be returned as characters.
        """
        phoneme_dict = (
            self._parse_as_cmu_dict(phoneme_dict, encoding)
            if isinstance(phoneme_dict, str) or isinstance(phoneme_dict, pathlib.Path) or phoneme_dict is None
            else phoneme_dict
        )

        if apply_to_oov_word is None:
            logging.warning(
                "apply_to_oov_word=None, This means that some of words will remain unchanged "
                "if they are not handled by any of the rules in self.parse_one_word(). "
                "This may be intended if phonemes and chars are both valid inputs, otherwise, "
                "you may see unexpected deletions in your input."
            )

        super().__init__(
            phoneme_dict=phoneme_dict,
            word_tokenize_func=word_tokenize_func,
            apply_to_oov_word=apply_to_oov_word,
            mapping_file=mapping_file,
        )

        self.ignore_ambiguous_words = ignore_ambiguous_words
        self.heteronyms = (
            set(self._parse_file_by_lines(heteronyms, encoding))
            if isinstance(heteronyms, str) or isinstance(heteronyms, pathlib.Path)
            else heteronyms
        )
        self.phoneme_probability = phoneme_probability
        self._rng = random.Random()

    @staticmethod
    def _parse_as_cmu_dict(phoneme_dict_path=None, encoding='latin-1'):
        if phoneme_dict_path is None:
            # this part of code downloads file, but it is not rank zero guarded
            # Try to check if torch distributed is available, if not get global rank zero to download corpora and make
            # all other ranks sleep for a minute
            if torch.distributed.is_available() and torch.distributed.is_initialized():
                group = torch.distributed.group.WORLD
                if is_global_rank_zero():
                    try:
                        nltk.data.find('corpora/cmudict.zip')
                    except LookupError:
                        nltk.download('cmudict', quiet=True)
                torch.distributed.barrier(group=group)
            elif is_global_rank_zero():
                logging.error(
                    f"Torch distributed needs to be initialized before you initialized EnglishG2p. This class is prone to "
                    "data access race conditions. Now downloading corpora from global rank 0. If other ranks pass this "
                    "before rank 0, errors might result."
                )
                try:
                    nltk.data.find('corpora/cmudict.zip')
                except LookupError:
                    nltk.download('cmudict', quiet=True)
            else:
                logging.error(
                    f"Torch distributed needs to be initialized before you initialized EnglishG2p. This class is prone to "
                    "data access race conditions. This process is not rank 0, and now going to sleep for 1 min. If this "
                    "rank wakes from sleep prior to rank 0 finishing downloading, errors might result."
                )
                time.sleep(60)

            logging.warning(
                f"English g2p_dict will be used from nltk.corpus.cmudict.dict(), because phoneme_dict_path=None. "
                "Note that nltk.corpus.cmudict.dict() has old version (0.6) of CMUDict. "
                "You can use the latest official version of CMUDict (0.7b) with additional changes from NVIDIA directly from NeMo "
                "using the path scripts/tts_dataset_files/cmudict-0.7b_nv22.10."
            )

            return nltk.corpus.cmudict.dict()

        _alt_re = re.compile(r'\([0-9]+\)')
        g2p_dict = {}
        with open(phoneme_dict_path, encoding=encoding) as file:
            for line in file:
                if len(line) and ('A' <= line[0] <= 'Z' or line[0] == "'"):
                    parts = line.split('  ')
                    word = re.sub(_alt_re, '', parts[0])
                    word = word.lower()

                    pronunciation = parts[1].strip().split(" ")
                    if word in g2p_dict:
                        g2p_dict[word].append(pronunciation)
                    else:
                        g2p_dict[word] = [pronunciation]
        return g2p_dict

    @staticmethod
    def _parse_file_by_lines(p, encoding):
        with open(p, encoding=encoding) as f:
            return [l.rstrip() for l in f.readlines()]

    def is_unique_in_phoneme_dict(self, word):
        return len(self.phoneme_dict[word]) == 1

    def parse_one_word(self, word: str):
        """
        Returns parsed `word` and `status` as bool.
        `status` will be `False` if word wasn't handled, `True` otherwise.
        """

        if self.phoneme_probability is not None and self._rng.random() > self.phoneme_probability:
            return word, True

        # punctuation or whitespace.
        if re.search(r"[a-zA-ZÀ-ÿ\d]", word) is None:
            return list(word), True

        # heteronyms
        if self.heteronyms is not None and word in self.heteronyms:
            return word, True

        # `'s` suffix
        if (
            len(word) > 2
            and word.endswith("'s")
            and (word not in self.phoneme_dict)
            and (word[:-2] in self.phoneme_dict)
            and (not self.ignore_ambiguous_words or self.is_unique_in_phoneme_dict(word[:-2]))
        ):
            return self.phoneme_dict[word[:-2]][0] + ["Z"], True

        # `s` suffix
        if (
            len(word) > 1
            and word.endswith("s")
            and (word not in self.phoneme_dict)
            and (word[:-1] in self.phoneme_dict)
            and (not self.ignore_ambiguous_words or self.is_unique_in_phoneme_dict(word[:-1]))
        ):
            return self.phoneme_dict[word[:-1]][0] + ["Z"], True

        # phoneme dict
        if word in self.phoneme_dict and (not self.ignore_ambiguous_words or self.is_unique_in_phoneme_dict(word)):
            return self.phoneme_dict[word][0], True

        if self.apply_to_oov_word is not None:
            return self.apply_to_oov_word(word), True
        else:
            return word, False

    def __call__(self, text):
        words = self.word_tokenize_func(text)

        prons = []
        for word, without_changes in words:
            if without_changes:
                prons.extend(word)
                continue

            word_str = word[0]
            word_by_hyphen = word_str.split("-")
            pron, is_handled = self.parse_one_word(word_str)

            if not is_handled and len(word_by_hyphen) > 1:
                pron = []
                for sub_word in word_by_hyphen:
                    p, _ = self.parse_one_word(sub_word)
                    pron.extend(p)
                    pron.extend(["-"])
                pron.pop()

            prons.extend(pron)

        return prons


@experimental
class IPAG2P(BaseG2p):
    # fmt: off
    STRESS_SYMBOLS = ["ˈ", "ˌ"]
    # Regex for roman characters, accented characters, and locale-agnostic numbers/digits
    CHAR_REGEX = re.compile(fr"[{LATIN_CHARS_ALL}\d]")
    PUNCT_REGEX = re.compile(fr"[^{LATIN_CHARS_ALL}\d]")
    # fmt: on

    def __init__(
        self,
        phoneme_dict: Union[str, pathlib.Path, dict],
        locale: str = "en-US",
        apply_to_oov_word: Optional[Callable[[str], str]] = None,
        ignore_ambiguous_words: bool = True,
        heteronyms: Optional[Union[str, pathlib.Path, List[str]]] = None,
        use_chars: bool = False,
        phoneme_probability: Optional[float] = None,
        use_stresses: Optional[bool] = True,
        grapheme_case: Optional[str] = GRAPHEME_CASE_UPPER,
        grapheme_prefix: Optional[str] = "",
        mapping_file: Optional[str] = None,
    ) -> None:
        """
        Generic IPA G2P module. This module converts words from graphemes to International Phonetic Alphabet
        representations. Optionally, it can ignore heteronyms, ambiguous words, or words marked as unchangeable
        by `word_tokenize_func` (see code for details). Ignored words are left unchanged or passed through
        `apply_to_oov_word` for handling.

        Args:
            phoneme_dict (str, Path, or Dict): Path to file in CMUdict format or an IPA dict object with CMUdict-like
                entries. For example,
                a dictionary file: scripts/tts_dataset_files/ipa_cmudict-0.7b_nv22.06.txt;
                a dictionary object: {..., "Wire": [["ˈ", "w", "a", "ɪ", "ɚ"], ["ˈ", "w", "a", "ɪ", "ɹ"]], ...}.
            locale (str): Locale used to determine a locale-specific tokenization logic. Currently, it supports "en-US",
                "de-DE", and "es-ES". Defaults to "en-US". Specify None if implementing custom logic for a new locale.
            apply_to_oov_word (Callable): Function that deals with the out-of-vocabulary (OOV) words that do not exist
                in the `phoneme_dict`.
            ignore_ambiguous_words (bool): Whether to handle word via phoneme_dict with ambiguous phoneme sequences.
                Defaults to True.
            heteronyms (str, Path, List[str]): Path to file that includes heteronyms (one word entry per line), or a
                list of words.
            use_chars (bool): Whether to include chars/graphemes in the token list. It is True if `phoneme_probability`
                is not None or if `apply_to_oov_word` function ever returns graphemes.
            phoneme_probability (Optional[float]): The probability (0.0 <= ε <= 1.0) that is used to balance the action
                that a word in a sentence is whether transliterated into a sequence of phonemes, or kept as a sequence
                of graphemes. If a random number for a word is greater than ε, then the word is kept as graphemes;
                otherwise, the word is transliterated as phonemes. Defaults to None which is equivalent to setting it
                to 1.0, meaning always transliterating the word into phonemes. Note that this code path is only run if
                the word can be transliterated into phonemes, otherwise, if a word does not have an entry in the g2p
                dict, it will be kept as graphemes. If a word has multiple pronunciations as shown in the g2p dict and
                `ignore_ambiguous_words` is True, it will be kept as graphemes as well.
            use_stresses (Optional[bool]): Whether to include the stress symbols (ˈ and ˌ).
            grapheme_case (Optional[str]): Trigger converting all graphemes to uppercase, lowercase, or keeping them as
                original mix-cases. You may want to use this feature to distinguish the grapheme set from the phoneme
                set if there is an overlap in between. Defaults to `upper` because phoneme set only uses lowercase
                symbols. You could explicitly prepend `grapheme_prefix` to distinguish them.
            grapheme_prefix (Optional[str]): Prepend a special symbol to any graphemes in order to distinguish graphemes
                from phonemes because there may be overlaps between the two set. It is suggested to choose a prefix that
                is not used or preserved somewhere else. "#" could be a good candidate. Default to "".
            TODO @borisfom: add docstring for newly added `mapping_file` argument.
        """
        self.use_stresses = use_stresses
        self.grapheme_case = grapheme_case
        self.grapheme_prefix = grapheme_prefix
        self.phoneme_probability = phoneme_probability
        self.locale = locale
        self._rng = random.Random()

        if locale is not None:
            validate_locale(locale)

        if not use_chars and self.phoneme_probability is not None:
            self.use_chars = True
            logging.warning(
                "phoneme_probability was not None, characters will be enabled even though "
                "use_chars was set to False."
            )
        else:
            self.use_chars = use_chars

        phoneme_dict_obj = self._parse_phoneme_dict(phoneme_dict)

        # verify if phoneme dict obj is empty
        if phoneme_dict_obj:
            self.phoneme_dict, self.symbols = self._normalize_dict(phoneme_dict_obj)
        else:
            raise ValueError(f"{phoneme_dict} contains no entries!")

        if apply_to_oov_word is None:
            logging.warning(
                "apply_to_oov_word=None, This means that some of words will remain unchanged "
                "if they are not handled by any of the rules in self.parse_one_word(). "
                "This may be intended if phonemes and chars are both valid inputs, otherwise, "
                "you may see unexpected deletions in your input."
            )

        # word_tokenize_func returns a List[Tuple[List[str], bool]] where every tuple denotes
        # a word representation (a list tokens) and a flag indicating whether to process the word or
        # leave it unchanged.
        if locale == "en-US":
            word_tokenize_func = english_word_tokenize
        else:
            word_tokenize_func = any_locale_word_tokenize

        super().__init__(
            phoneme_dict=self.phoneme_dict,
            word_tokenize_func=word_tokenize_func,
            apply_to_oov_word=apply_to_oov_word,
            mapping_file=mapping_file,
        )

        self.ignore_ambiguous_words = ignore_ambiguous_words
        if isinstance(heteronyms, str) or isinstance(heteronyms, pathlib.Path):
            self.heteronyms = set(self._parse_file_by_lines(heteronyms))
        elif isinstance(heteronyms, list) and all(isinstance(het, str) for het in heteronyms):
            self.heteronyms = set(heteronyms)
        else:
            self.heteronyms = None

        if self.heteronyms:
            self.heteronyms = {set_grapheme_case(het, case=self.grapheme_case) for het in self.heteronyms}

    @staticmethod
    def _parse_phoneme_dict(
        phoneme_dict: Union[str, pathlib.Path, Dict[str, List[List[str]]]]
    ) -> Dict[str, List[List[str]]]:
        """
        parse an input IPA dictionary and save it as a dict object.

        Args:
            phoneme_dict (Union[str, pathlib.Path, dict]): Path to file in CMUdict format or an IPA dict object with
                CMUdict-like entries. For example,
                a dictionary file: scripts/tts_dataset_files/ipa_cmudict-0.7b_nv22.06.txt;
                a dictionary object: {..., "Wire": [["ˈ", "w", "a", "ɪ", "ɚ"], ["ˈ", "w", "a", "ɪ", "ɹ"]], ...}.

        Returns: a dict object (Dict[str, List[List[str]]]).
        """
        if isinstance(phoneme_dict, str) or isinstance(phoneme_dict, pathlib.Path):
            # load the dictionary file where there may exist a digit suffix after a word, e.g. "Word(2)", which
            # represents the pronunciation variant of that word.
            phoneme_dict_obj = defaultdict(list)
            _alt_re = re.compile(r"\([0-9]+\)")
            with open(phoneme_dict, "r") as fdict:
                for line in fdict:
                    # skip the empty lines
                    if len(line) == 0:
                        continue

                    # Note that latin character pattern should be consistent with
                    # nemo_text_processing.g2p.data.data_utils.LATIN_CHARS_ALL. It is advised to extend its character
                    # coverage if adding the support of new languages.
                    # TODO @xueyang: unify hardcoded range of characters with LATIN_CHARS_ALL to avoid duplicates.
                    line = normalize_unicode_text(line)

                    if (
                        'A' <= line[0] <= 'Z'
                        or 'a' <= line[0] <= 'z'
                        or 'À' <= line[0] <= 'Ö'
                        or 'Ø' <= line[0] <= 'ö'
                        or 'ø' <= line[0] <= 'ÿ'
                        or line[0] == "'"
                    ):
                        parts = line.strip().split(maxsplit=1)
                        word = re.sub(_alt_re, "", parts[0])
                        prons = re.sub(r"\s+", "", parts[1])
                        phoneme_dict_obj[word].append(list(prons))
        else:
            # Load phoneme_dict as dictionary object
            logging.info("Loading phoneme_dict as a Dict object, and validating its entry format.")

            phoneme_dict_obj = {}
            for word, prons in phoneme_dict.items():
                # validate dict entry format
                assert isinstance(
                    prons, list
                ), f"Pronunciation type <{type(prons)}> is not supported. Please convert to <list>."

                # normalize word with NFC form
                word = normalize_unicode_text(word)

                # normalize phonemes with NFC form
                prons = [[normalize_unicode_text(p) for p in pron] for pron in prons]

                phoneme_dict_obj.update({word: prons})

        return phoneme_dict_obj

    def replace_dict(self, phoneme_dict: Union[str, pathlib.Path, Dict[str, List[List[str]]]]):
        """
        Replace model's phoneme dictionary with a custom one
        """
        self.phoneme_dict = self._parse_phoneme_dict(phoneme_dict)

    @staticmethod
    def _parse_file_by_lines(p: Union[str, pathlib.Path]) -> List[str]:
        with open(p, 'r') as f:
            return [line.rstrip() for line in f.readlines()]

    def _prepend_prefix_for_one_word(self, word: str) -> List[str]:
        return [f"{self.grapheme_prefix}{character}" for character in word]

    def _normalize_dict(self, phoneme_dict_obj: Dict[str, List[List[str]]]) -> Tuple[Dict[str, List[List[str]]], Set]:
        """
        Parse a python dict object according to the decision on word cases and removal of lexical stress markers.

        Args:
            phoneme_dict_obj (Dict[str, List[List[str]]]): a dictionary object.
                e.g. {..., "Wire": [["ˈ", "w", "a", "ɪ", "ɚ"], ["ˈ", "w", "a", "ɪ", "ɹ"]], ...}

        Returns:
            g2p_dict (dict): processed dict.
            symbols (set): a IPA phoneme set, or its union with grapheme set.

        """
        g2p_dict = defaultdict(list)
        symbols = set()
        for word, prons in phoneme_dict_obj.items():
            # process word
            # update word cases.
            word_new = set_grapheme_case(word, case=self.grapheme_case)

            # add grapheme symbols if `use_chars=True`.
            if self.use_chars:
                # remove punctuations within a word. Punctuations can exist at the start, middle, and end of a word.
                word_no_punct = self.PUNCT_REGEX.sub('', word_new)

                # add prefix to distinguish graphemes from phonemes.
                symbols.update(self._prepend_prefix_for_one_word(word_no_punct))

            # process IPA pronunciations
            # update phoneme symbols by removing lexical stress markers if `use_stresses=False`.
            prons_new = list()
            if not self.use_stresses:
                for pron in prons:
                    prons_new.append([symbol for symbol in pron if symbol not in self.STRESS_SYMBOLS])
            else:
                prons_new = prons

            # update symbols
            for pron in prons_new:
                symbols.update(pron)  # This will insert each char individually

            # update dict entry
            g2p_dict[word_new] = prons_new

            # duplicate word entries if grapheme_case is mixed. Even though grapheme_case is set to mixed, the words in
            # the original input text and the g2p_dict remain unchanged, so they could still be either lowercase,
            # uppercase, or mixed-case as defined in `set_grapheme_case` func. When mapping an uppercase word, e.g.
            # "HELLO", into phonemes using the g2p_dict with {"Hello": [["həˈɫoʊ"]]}, "HELLO" can't find its
            # pronunciations in the g2p_dict due to the case-mismatch of the words. Augmenting the g2p_dict with its
            # uppercase word entry, e.g. {"Hello": [["həˈɫoʊ"]], "HELLO": [["həˈɫoʊ"]]} would provide possibility to
            # find "HELLO"'s pronunciations rather than directly considering it as an OOV.
            if self.grapheme_case == GRAPHEME_CASE_MIXED and not word_new.isupper():
                g2p_dict[word_new.upper()] = prons_new

        return g2p_dict, symbols

    # TODO @xueyang: deprecate this function because it is useless. If unknown graphemes appear, then apply_to_oov_words
    #   should handle it.
    def add_symbols(self, symbols: str) -> None:
        """By default, the G2P symbols will be inferred from the words & pronunciations in the phoneme_dict.
        Use this to add characters in the vocabulary that are not present in the phoneme_dict.
        """
        symbols = normalize_unicode_text(symbols)
        self.symbols.update(symbols)

    def is_unique_in_phoneme_dict(self, word: str) -> bool:
        return len(self.phoneme_dict[word]) == 1

    def parse_one_word(self, word: str) -> Tuple[List[str], bool]:
        """Returns parsed `word` and `status` (bool: False if word wasn't handled, True otherwise).
        """
        word = set_grapheme_case(word, case=self.grapheme_case)

        # Punctuation (assumes other chars have been stripped)
        if self.CHAR_REGEX.search(word) is None:
            return list(word), True

        # Keep graphemes of a word with a probability.
        if self.phoneme_probability is not None and self._rng.random() > self.phoneme_probability:
            return self._prepend_prefix_for_one_word(word), True

        # Heteronyms
        if self.heteronyms and word in self.heteronyms:
            return self._prepend_prefix_for_one_word(word), True

        # special cases for en-US when transliterating a word into a list of phonemes.
        # TODO @xueyang: add special cases for any other languages upon new findings.
        if self.locale == "en-US":
            # `'s` suffix (with apostrophe) - not in phoneme dict
            if len(word) > 2 and (word.endswith("'s") or word.endswith("'S")):
                word_found = None
                if (word not in self.phoneme_dict) and (word.upper() not in self.phoneme_dict):
                    if word[:-2] in self.phoneme_dict:
                        word_found = word[:-2]
                    elif word[:-2].upper() in self.phoneme_dict:
                        word_found = word[:-2].upper()

                if word_found is not None and (
                    not self.ignore_ambiguous_words or self.is_unique_in_phoneme_dict(word_found)
                ):
                    if word_found[-1] in ['T', 't']:
                        # for example, "airport's" doesn't exist in the dict while "airport" exists. So append a phoneme
                        # /s/ at the end of "airport"'s first pronunciation.
                        return self.phoneme_dict[word_found][0] + ["s"], True
                    elif word_found[-1] in ['S', 's']:
                        # for example, "jones's" doesn't exist in the dict while "jones" exists. So append two phonemes,
                        # /ɪ/ and /z/ at the end of "jones"'s first pronunciation.
                        return self.phoneme_dict[word_found][0] + ["ɪ", "z"], True
                    else:
                        return self.phoneme_dict[word_found][0] + ["z"], True

            # `s` suffix (without apostrophe) - not in phoneme dict
            if len(word) > 1 and (word.endswith("s") or word.endswith("S")):
                word_found = None
                if (word not in self.phoneme_dict) and (word.upper() not in self.phoneme_dict):
                    if word[:-1] in self.phoneme_dict:
                        word_found = word[:-1]
                    elif word[:-1].upper() in self.phoneme_dict:
                        word_found = word[:-1].upper()

                if word_found is not None and (
                    not self.ignore_ambiguous_words or self.is_unique_in_phoneme_dict(word_found)
                ):
                    if word_found[-1] in ['T', 't']:
                        # for example, "airports" doesn't exist in the dict while "airport" exists. So append a phoneme
                        # /s/ at the end of "airport"'s first pronunciation.
                        return self.phoneme_dict[word_found][0] + ["s"], True
                    else:
                        return self.phoneme_dict[word_found][0] + ["z"], True

        # For the words that have a single pronunciation, directly look it up in the phoneme_dict; for the
        # words that have multiple pronunciation variants, if we don't want to ignore them, then directly choose their
        # first pronunciation variant as the target phonemes.
        # TODO @xueyang: this is a temporary solution, but it is not optimal if always choosing the first pronunciation
        #  variant as the target if a word has multiple pronunciation variants. We need explore better approach to
        #  select its optimal pronunciation variant aligning with its reference audio.
        if word in self.phoneme_dict and (not self.ignore_ambiguous_words or self.is_unique_in_phoneme_dict(word)):
            return self.phoneme_dict[word][0], True

        if (
            self.grapheme_case == GRAPHEME_CASE_MIXED
            and word not in self.phoneme_dict
            and word.upper() in self.phoneme_dict
        ):
            word = word.upper()
            if not self.ignore_ambiguous_words or self.is_unique_in_phoneme_dict(word):
                return self.phoneme_dict[word][0], True

        if self.apply_to_oov_word is not None:
            return self.apply_to_oov_word(word), True
        else:
            return self._prepend_prefix_for_one_word(word), False

    def __call__(self, text: str) -> List[str]:
        text = normalize_unicode_text(text)

        if self.heteronym_model is not None:
            try:
                text = self.heteronym_model.disambiguate(sentences=[text])[1][0]
            except Exception as e:
                logging.warning(f"Heteronym model failed {e}, skipping")

        words_list_of_tuple = self.word_tokenize_func(text)

        prons = []
        for words, without_changes in words_list_of_tuple:
            if without_changes:
                # for example: (["NVIDIA", "unchanged"], True). "NVIDIA" is considered as a single token.
                prons.extend([f"{self.grapheme_prefix}{word}" for word in words])
            else:
                assert (
                    len(words) == 1
                ), f"{words} should only have a single item when `without_changes` is False, but found {len(words)}."

                word = words[0]
                pron, is_handled = self.parse_one_word(word)

                # If `is_handled` is False, then the only possible case is that the word is an OOV. The OOV may have a
                # hyphen so that it doesn't show up in the g2p dictionary. We need split it into sub-words by a hyphen,
                # and parse the sub-words again just in case any sub-word exists in the g2p dictionary.
                if not is_handled:
                    subwords_by_hyphen = word.split("-")
                    if len(subwords_by_hyphen) > 1:
                        pron = []  # reset the previous pron
                        for sub_word in subwords_by_hyphen:
                            p, _ = self.parse_one_word(sub_word)
                            pron.extend(p)
                            pron.append("-")
                        pron.pop()  # remove the redundant hyphen that is previously appended at the end of the word.

                prons.extend(pron)

        return prons


class ChineseG2p(BaseG2p):
    def __init__(
        self,
        phoneme_dict=None,
        word_tokenize_func=None,
        apply_to_oov_word=None,
        mapping_file: Optional[str] = None,
        word_segmenter: Optional[str] = None,
    ):
        """Chinese G2P module. This module first converts Chinese characters into pinyin sequences using pypinyin, then pinyin sequences would
           be further converted into phoneme sequences using pinyin_dict_nv_22.10.txt dict file. For Chinese and English bilingual sentences, the English words
           would be converted into letters.
        Args:
            phoneme_dict (str, Path, Dict): Path to pinyin_dict_nv_22.10.txt dict file.
            word_tokenize_func: Function for tokenizing text to words.
                It has to return List[Tuple[Union[str, List[str]], bool]] where every tuple denotes word representation and flag whether to leave unchanged or not.
                It is expected that unchangeable word representation will be represented as List[str], other cases are represented as str.
                It is useful to mark word as unchangeable which is already in phoneme representation.
            apply_to_oov_word: Function that will be applied to out of phoneme_dict word.
            word_segmenter: method that will be applied to segment utterances into words for better polyphone disambiguation.
        """
        assert phoneme_dict is not None, "Please set the phoneme_dict path."
        assert word_segmenter in [
            None,
            'jieba',
        ], f"{word_segmenter} is not supported now. Please choose correct word_segmenter."

        phoneme_dict = (
            self._parse_as_pinyin_dict(phoneme_dict)
            if isinstance(phoneme_dict, str) or isinstance(phoneme_dict, pathlib.Path)
            else phoneme_dict
        )

        if apply_to_oov_word is None:
            logging.warning(
                "apply_to_oov_word=None, This means that some of words will remain unchanged "
                "if they are not handled by any of the rules in self.parse_one_word(). "
                "This may be intended if phonemes and chars are both valid inputs, otherwise, "
                "you may see unexpected deletions in your input."
            )

        super().__init__(
            phoneme_dict=phoneme_dict,
            word_tokenize_func=word_tokenize_func,
            apply_to_oov_word=apply_to_oov_word,
            mapping_file=mapping_file,
        )
        self.tones = {'1': '#1', '2': '#2', '3': '#3', '4': '#4', '5': '#5'}

        if word_segmenter == "jieba":
            try:
                import jieba
            except ImportError as e:
                logging.error(e)

            # Cut sentences into words to improve polyphone disambiguation
            self.word_segmenter = jieba.cut
        else:
            self.word_segmenter = lambda x: [x]

        try:
            from pypinyin import lazy_pinyin, Style
            from pypinyin_dict.pinyin_data import cc_cedict
        except ImportError as e:
            logging.error(e)

        # replace pypinyin default dict with cc_cedict.txt for polyphone disambiguation
        cc_cedict.load()

        self._lazy_pinyin = lazy_pinyin
        self._Style = Style

    @staticmethod
    def _parse_as_pinyin_dict(phoneme_dict_path):
        """Loads pinyin dict file, and generates a set of all valid symbols."""
        g2p_dict = defaultdict(list)
        with open(phoneme_dict_path, 'r') as file:
            for line in file:
                parts = line.split('\t')
                # let the key be lowercased, since pypinyin would give lower representation
                pinyin = parts[0].lower()
                pronunciation = parts[1].split()
                pronunciation_with_sharp = ['#' + pron for pron in pronunciation]
                g2p_dict[pinyin] = pronunciation_with_sharp
        return g2p_dict

    def __call__(self, text):
        """
        errors func handle below is to process the bilingual situation,
        where English words would be split into letters.
        e.g. 我今天去了Apple Store, 买了一个iPhone。
        would return a list
        ['wo3', 'jin1', 'tian1', 'qu4', 'le5', 'A', 'p', 'p', 'l', 'e',
        ' ', 'S', 't', 'o', 'r', 'e', ',', ' ', 'mai3', 'le5', 'yi2',
        'ge4', 'i', 'P', 'h', 'o', 'n', 'e', '。']
        """
        pinyin_seq = []
        words_list = self.word_segmenter(text)

        for word in words_list:
            pinyin_seq += self._lazy_pinyin(
                word,
                style=self._Style.TONE3,
                neutral_tone_with_five=True,
                errors=lambda en_words: [letter for letter in en_words],
            )
        phoneme_seq = []
        for pinyin in pinyin_seq:
            if pinyin[-1] in self.tones:
                assert pinyin[:-1] in self.phoneme_dict, pinyin[:-1]
                phoneme_seq += self.phoneme_dict[pinyin[:-1]]
                phoneme_seq.append(self.tones[pinyin[-1]])
            # All pinyin would end up with a number in 1-5, which represents tones of the pinyin.
            # For symbols which are not pinyin, e.g. English letters, Chinese puncts, we directly
            # use them as inputs.
            else:
                phoneme_seq.append(pinyin)
        return phoneme_seq

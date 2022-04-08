# Copyright 2021 Yuan He (KRR-Oxford). All rights reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Class for ontology alignment, requiring:

1. Mapping computation for a cross-ontology entity pair;
2. Full alignment computation w/o selection heuristic.

"""

from itertools import cycle, chain
from typing import List, Tuple, Optional, Iterable
from multiprocessing_on_dill import Process, Manager
from pyats.datastructures import AttrDict
import numpy as np
import os

from deeponto.onto import Ontology
from deeponto.onto.mapping import *
from deeponto.onto.text import Tokenizer, text_utils
from deeponto.utils.logging import create_logger, banner_msg
from deeponto.utils import detect_path


class OntoAlign:
    def __init__(
        self,
        src_onto: Ontology,
        tgt_onto: Ontology,
        tokenizer: Tokenizer,
        cand_pool_size: Optional[int] = 200,
        rel: str = "≡",
        n_best: Optional[int] = 10,
        is_trainable: bool = False,
        saved_path: str = "",
    ):

        self.src_onto = src_onto
        self.tgt_onto = tgt_onto
        self.tokenizer = tokenizer
        self.cand_pool_size = cand_pool_size
        self.rel = rel
        self.saved_path = os.path.abspath(saved_path)  # absolute path is needed for java repair
        self.set_mapping = lambda src_ent_name, tgt_ent_name, mapping_score: EntityMapping(
            src_ent_name, tgt_ent_name, self.rel, mapping_score
        )
        self.new_mapping_list = lambda: EntityMappingList()
        self.logger = create_logger(f"{type(self).__name__}", saved_path=self.saved_path)
        self.n_best = n_best
        self.is_trainable = is_trainable

        self.src2tgt_mappings = self.load_mappings("src2tgt", "global_match")
        self.tgt2src_mappings = self.load_mappings("tgt2src", "global_match")
        self.flag_set = cycle(["src2tgt", "tgt2src"])
        self.flag = next(self.flag_set)

    ##################################################################################
    ###                        compute entity pair mappings                        ###
    ##################################################################################

    def pair_score(self, tbc_mappings: OntoMappings, flag: str):
        """Compute mappings for intput src-tgt entity pairs
        """
        self.logger.info(
            f'Pair-score and rank input "{self.rel}" Mappings: {self.src_onto.owl.name} ==> {self.tgt_onto.owl.name}\n'
        )
        # change side according to given
        while not self.flag == flag:
            self.switch()
        prefix = self.flag.split("2")[1]  # src or tgt
        # maximum number of mappings is the number of opposite ontology classes
        max_num_mappings = len(getattr(self, f"{prefix}_onto").idx2class)
        # temp = self.n_best
        self.n_best = max_num_mappings  # change n_best to all possible mappings
        mappings = self.load_mappings(flag, "pair_score")
        # self.n_best = temp
        for src_ent_name, tgt2score in tbc_mappings.ranked.items():
            src_ent_id = self.src_onto.class2idx[src_ent_name]
            for tgt_ent_name in tgt2score.keys():
                tgt_ent_id = self.tgt_onto.class2idx[tgt_ent_name]
                score = self.ent_pair_score(src_ent_id, tgt_ent_id)
                mappings.add(EntityMapping(src_ent_name, tgt_ent_name, self.rel, score))
        self.logger.info("Task Finished\n")
        mappings.save_instance(f"{self.saved_path}/pair_score/{self.flag}")

    def ent_pair_score(self, src_ent_id: str, tgt_ent_id: str) -> float:
        """Compute mapping score between a cross-ontology entity pair
        """
        raise NotImplementedError

    ##################################################################################
    ###                        compute global mappings                             ###
    ##################################################################################

    def global_match(self, num_procs: Optional[int] = None):
        """Compute alignment for both src2tgt and tgt2src
        """
        self.renew()
        # if not detect_path(f"{self.saved_path}/global_match/src2tgt"):
        self.global_mappings_for_onto_multi_procs(
            num_procs
        ) if num_procs else self.global_mappings_for_onto()
        # else:
        #     print("found saved src2tgt mappings; delete it and re-run if empty or incomplete ...")
        self.switch()
        # if not detect_path(f"{self.saved_path}/global_match/tgt2src"):
        self.global_mappings_for_onto_multi_procs(
            num_procs
        ) if num_procs else self.global_mappings_for_onto()
        # else:
        #    print("found saved tgt2src mappings; delete it and re-run if empty or incomplete ...")
        self.renew()

    def renew(self):
        """Renew alignment direction to src2tgt
        """
        while self.flag != "src2tgt":
            self.switch()

    def switch(self):
        """Switch alignment direction
        """
        self.src_onto, self.tgt_onto = self.tgt_onto, self.src_onto
        self.flag = next(self.flag_set)

    def current_global_mappings(self):
        return getattr(self, f"{self.flag}_mappings")

    def load_mappings(self, flag: str, mode: str):
        """Create a new OntoMappings or load from saved one if any ...
        """
        flag_mappings = OntoMappings(flag=flag, n_best=self.n_best, rel=self.rel)
        saved_mappigs_path = f"{self.saved_path}/{mode}/{flag}"
        if detect_path(saved_mappigs_path):
            flag_mappings = OntoMappings.from_saved(saved_mappigs_path)
            print(f"found existing {flag} mappings, skip predictions for the saved classes ...")
        return flag_mappings

    def global_mappings_for_onto_multi_procs(self, num_procs: int):
        """Compute mappings for all entities in the current source ontology but distributed
        to multiple processes
        """
        # manager for collecting mappings from different procs
        manager = Manager()
        return_dict = manager.dict()
        # suggested by huggingface when doing multi-threading
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        def async_compute(proc_idx: int, return_dict: dict, src_ent_id_chunk: Iterable[int]):
            return_dict[proc_idx] = self.global_mappings_for_ent_chunk(
                src_ent_id_chunk, intermediate_saving=False
            )

        self.logger.info(
            f'Compute "{self.rel}" Mappings: {self.src_onto.owl.name} ==> {self.tgt_onto.owl.name}\n'
        )

        # split entity ids into {num_procs} chunks
        src_ent_id_chunks = np.array_split(list(self.src_onto.idx2class.keys()), num_procs)

        # start proc for each chunk
        jobs = []
        for i in range(num_procs):
            p = Process(target=async_compute, args=(i, return_dict, src_ent_id_chunks[i]))
            jobs.append(p)
            p.start()

        # block the main thread until all procs finished
        for p in jobs:
            p.join()

        # save the output mappings
        mappings = self.current_global_mappings()
        for ent_mappings in return_dict.values():
            mappings.add_many(*ent_mappings)
        self.logger.info("Task Finished\n")
        mappings.save_instance(f"{self.saved_path}/global_match/{self.flag}")

    def global_mappings_for_onto(self):
        """Compute mappings for all entities in the current source ontology
        """
        self.logger.info(
            f'Compute "{self.rel}" Mappings: {self.src_onto.owl.name} ==> {self.tgt_onto.owl.name}\n'
        )
        # save the output mappings
        self.global_mappings_for_ent_chunk(self.src_onto.idx2class.keys())
        self.logger.info("Task Finished\n")
        # saving the last batch
        mappings = self.current_global_mappings()
        mappings.save_instance(f"{self.saved_path}/global_match/{self.flag}")

    def global_mappings_for_ent_chunk(
        self,
        src_ent_id_chunk: Iterable[int],
        save_step: int = 100,
        intermediate_saving: bool = True,
    ):
        """Compute cross-ontology mappings for a chunk of source entities,
        Note: save time especially for evaluating on Hits@K, MRR, etc.
        """
        mappings = self.current_global_mappings()
        mappings_for_chunk = []
        for src_ent_id in src_ent_id_chunk:
            src_ent_name = self.src_onto.idx2class[src_ent_id]
            if src_ent_name in mappings.ranked.keys():
                self.logger.info(f"skip prediction for {src_ent_name} as already computed ...")
                continue
            cur_mappings = self.global_mappings_for_ent(src_ent_id)
            mappings.add_many(*cur_mappings)
            mappings_for_chunk.append(cur_mappings)
            if intermediate_saving and src_ent_id % save_step == 0:
                mappings.save_instance(f"{self.saved_path}/global_match/{self.flag}")
                self.logger.info("Save currently computed mappings ...")
        return mappings_for_chunk

    def global_mappings_for_ent(self, src_ent_id: int) -> EntityMappingList:
        """Compute cross-ontology mappings for a source entity
        """
        banner_msg(f"Compute Mappings for Entity {src_ent_id} ({self.flag})")
        mappings_for_ent = self.new_mapping_list()
        # TODO: followed by individual implementations
        return mappings_for_ent

    def idf_select_for_ent(self, src_ent_id: int) -> Tuple[str, float]:
        """Select candidates in target ontology for a given source entity
        """
        src_ent_labs = self.src_onto.idx2labs[src_ent_id]
        src_ent_toks = self.tokenizer.tokenize_all(src_ent_labs)
        # TODO: could have more candidate selection methods in future
        tgt_cand_ids = self.tgt_onto.idf_select(
            src_ent_toks, self.cand_pool_size
        )  # [(ent_id, idf_score)]
        return tgt_cand_ids

    ##################################################################################
    ###                        other auxiliary functions                           ###
    ##################################################################################

    def lab_products_for_ent(
        self, src_ent_id: int, tgt_cands: List[Tuple[str, float]]
    ) -> Tuple[List[Tuple[str, str]], List[int]]:
        """Compute Catesian Product between a source entity's labels and its selected 
        target entities' labels, with each block length recorded
        """
        src_sents, tgt_sents = [], []
        product_lens = []
        src_ent_labs = self.src_onto.idx2labs[src_ent_id]
        # tgt_cands = self.idf_select_for_ent(src_ent_id)
        for tgt_cand_id, _ in tgt_cands:
            tgt_ent_labs = self.tgt_onto.idx2labs[tgt_cand_id]
            src_out, tgt_out = text_utils.lab_product(src_ent_labs, tgt_ent_labs)
            assert len(src_out) == len(tgt_out)
            product_lens.append(len(src_out))
            src_sents += src_out
            tgt_sents += tgt_out
        return list(zip(src_sents, tgt_sents)), product_lens

    def batched_lab_products_for_ent(
        self, src_ent_id: int, tgt_cands: List[Tuple[str, float]], batch_size: int
    ):
        """Compute the batched Catesian Product between a source entity's labels and its selected 
        target entities' labels; batches are distributed according to block lengths
        """
        lab_products, product_lens = self.lab_products_for_ent(src_ent_id, tgt_cands)
        batches = []
        cur_batch = AttrDict({"labs": [], "lens": []})
        cur_lab_pointer = 0
        for i in range(len(product_lens)):  # which is the size of candidate pool
            cur_length = product_lens[i]
            cur_labs = lab_products[cur_lab_pointer : cur_lab_pointer + cur_length]
            cur_batch.labs += cur_labs
            cur_batch.lens.append(cur_length)
            # collect when the batch is full or for the last set of label pairs
            if sum(cur_batch.lens) > batch_size or i == len(product_lens) - 1:
                # deep copy is necessary for dictionary data
                batches.append(cur_batch)
                cur_batch = AttrDict({"labs": [], "lens": []})
            cur_lab_pointer += cur_length
        # small check for the algorithm
        assert lab_products == list(chain.from_iterable([b.labs for b in batches]))
        assert product_lens == list(chain.from_iterable([b.lens for b in batches]))
        return batches

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
"""Class for ontology alignment pipeline"""

from itertools import cycle
from typing import List, Tuple, Optional, Iterable
from multiprocessing_on_dill import Process, Manager
from copy import deepcopy
import numpy as np
import os

from deeponto.onto import Ontology
from deeponto.onto.mapping import *
from deeponto.onto.onto_text import Tokenizer, text_utils
from deeponto.utils import banner_msg
from deeponto.utils.logging import create_logger


class OntoAlign:
    def __init__(
        self,
        src_onto: Ontology,
        tgt_onto: Ontology,
        tokenizer: Tokenizer,
        cand_pool_size: Optional[int] = 200,
        rel: str = "≡",
        n_best: Optional[int] = 10,
        saved_path: str = "",
    ):

        self.src_onto = src_onto
        self.tgt_onto = tgt_onto
        self.tokenizer = tokenizer
        self.cand_pool_size = cand_pool_size
        self.rel = rel
        self.saved_path = saved_path
        self.set_mapping = lambda src_ent_name, tgt_ent_name, mapping_score: EntityMapping(
            src_ent_name, tgt_ent_name, self.rel, mapping_score
        )
        self.new_mapping_list = lambda: EntityMappingList()
        self.logger = create_logger(f"{type(self).__name__}", saved_path=self.saved_path)
        self.n_best = n_best

        self.src2tgt_mappings = Alignment(flag="src2tgt", n_best=self.n_best, rel=self.rel)
        self.tgt2src_mappings = Alignment(flag="tgt2src", n_best=self.n_best, rel=self.rel)
        self.combined_mappings = None
        self.flag_set = cycle(["src2tgt", "tgt2src"])
        self.flag = next(self.flag_set)

    def run(self, num_procs: Optional[int] = None):
        """Compute alignment for both src2tgt and tgt2src
        """
        self.compute_mappings_all_multi_procs(
            num_procs
        ) if num_procs else self.compute_mappings_all()
        self.switch()
        self.compute_mappings_all_multi_procs(
            num_procs
        ) if num_procs else self.compute_mappings_all()
        self.combined_alignment()
        
    # def combined_alignment(self):
    #     """Combine src2tgt and tgt2src mappings with duplicates removed
    #     """
    #     while self.flag != "src2tgt":
    #         self.switch()
    #     # add all the tgt2src mappings into the deep-copied src2tgt mappings
    #     # since mappings are maintained in dict, duplicates will automatically be removed 
    #     self.combined_mappings = deepcopy(self.src2tgt_mappings)
    #     self.combined_mappings.flag = "combined"
    #     self.combined_mappings.saved_name = f"combined.maps"
    #     for tgt_ent_name, v in self.tgt2src_mappings.ranked.items():
    #         for src_ent_name, score in v.items():
    #             em = EntityMapping(src_ent_name, tgt_ent_name, self.rel, score)
    #             if self.src2tgt_mappings.check_existed(em)
    #                 self.combined_mappings.add(EntityMapping(tgt_ent_name, src_ent_name, self.rel, score))
    #     self.combined_mappings.save_instance(f"{self.saved_path}/combined")

    def switch(self):
        """Switch alignment direction
        """
        self.src_onto, self.tgt_onto = self.tgt_onto, self.src_onto
        self.flag = next(self.flag_set)

    def current_mappings(self):
        return getattr(self, f"{self.flag}_mappings")

    def compute_mappings_all_multi_procs(self, num_procs: int):
        """Compute mappings for all entities in the current source ontology but distributed
        to multiple processes
        """
        # manager for collecting mappings from different procs
        manager = Manager()
        return_dict = manager.dict()
        # suggested by huggingface when doing multi-threading
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

        def async_compute(proc_idx: int, return_dict: dict, src_ent_id_chunk: Iterable[int]):
            return_dict[proc_idx] = self.compute_mappings_chunk(src_ent_id_chunk)

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
        mappings = self.current_mappings()
        for ent_mappings in return_dict.values():
            mappings.add_many(*ent_mappings)
        banner_msg("Task Finished")
        mappings.save_instance(f"{self.saved_path}/{self.flag}")

    def compute_mappings_all(self):
        """Compute mappings for all entities in the current source ontology
        """
        self.logger.info(
            f'Compute "{self.rel}" Mappings: {self.src_onto.owl.name} ==> {self.tgt_onto.owl.name}\n'
        )
        # save the output mappings
        mappings = self.current_mappings()
        mappings.add_many(*self.compute_mappings_chunk(self.src_onto.idx2class.keys()))
        banner_msg("Task Finished")
        mappings.save_instance(f"{self.saved_path}/{self.flag}")

    def compute_mappings_chunk(self, src_ent_id_chunk: Iterable[int]):
        """Compute cross-ontology mappings for a chunk of source entities
        """
        mappings_for_chunk = []
        for src_ent_id in src_ent_id_chunk:
            mappings_for_chunk += self.compute_mappings_for_ent(src_ent_id)
        return mappings_for_chunk

    def compute_mappings_for_ent(self, src_ent_id: int) -> EntityMappingList:
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
        tgt_cands = self.tgt_onto.idf_select(
            src_ent_toks, self.cand_pool_size
        )  # [(ent_id, idf_score)]
        return tgt_cands

    def lab_products_for_ent(self, src_ent_id: int) -> Tuple[List[str], List[str], List[int]]:
        """Compute Catesian Product between a source entity's labels and its selected 
        target entities' labels, with each block length recorded
        """
        src_sents, tgt_sents = [], []
        product_lens = []
        src_ent_labs = self.src_onto.idx2labs[src_ent_id]
        tgt_cands = self.idf_select_for_ent(src_ent_id)
        for tgt_cand_id, _ in tgt_cands:
            tgt_ent_labs = self.tgt_onto.idx2labs[tgt_cand_id]
            src_out, tgt_out = text_utils.lab_product(src_ent_labs, tgt_ent_labs)
            assert len(src_out) == len(tgt_out)
            product_lens.append(len(src_out))
            src_sents += src_out
            tgt_sents += tgt_out
        return src_sents, tgt_sents, product_lens

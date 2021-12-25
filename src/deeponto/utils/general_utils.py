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
"""Providing useful utility functions"""
from typing import Optional
import random
import re
import sys

##################################################################################
###                             element processing                             ###
##################################################################################


def uniqify(ls):
    """Return a list of unique elements without messing around the order
    """
    non_empty_ls = list(filter(lambda x: x != "", ls))
    return list(dict.fromkeys(non_empty_ls))


def to_identifier(var_name: str):
    """Change a variable name to a valid identifier
    """
    if var_name.isidentifier():
        return var_name
    else:
        changed_name = "".join(re.findall(r"[a-zA-Z_]+[0-9]*", var_name))
        print(f"change invalid identifier name: {var_name} ==> {changed_name}")
        return changed_name


def sort_dict_by_values(dic: dict, desc: bool = True, top_k: Optional[int] = None):
    """Return a sorted dict by values with top k reserved
    """
    top_k = len(dic) if not top_k else top_k
    sorted_items = list(sorted(dic.items(), key=lambda item: item[1], reverse=desc))
    return dict(sorted_items[:top_k])


##################################################################################
###                                 randomness                                 ###
##################################################################################


def rand_sample_excl(start, end, number, *excl):
    """Randomly generate a number between {start} and {end} with end and specified 
    {excl} value(s) excluded
    """
    field = list(set(range(start, end)) - set(excl))
    if not field:
        raise ValueError(f"impossible to generate a number because the whole range is excluded")
    return random.sample(field, number)


##################################################################################
###                                   logging                                  ###
##################################################################################


def log_print(log_info, log_path: str):
    """Print and save log information
    """
    print(log_info)
    with open(log_path, "a+") as f:
        f.write(f"{log_info}\n")
    # flush() is important for printing logs during multiprocessing
    sys.stdout.flush()


def banner_msg(message: str, banner_len: int = 70, sym="#"):
    """Print banner message:
    
    ######################################################################
    ###                            example                             ###
    ######################################################################
    
    """
    print()
    print(sym * banner_len)
    message = sym * 3 + " " * ((banner_len - len(message)) // 2 - 3) + message
    message = message + " " * (banner_len - len(message) - 3) + sym * 3
    print(message)
    print(sym * banner_len)
    print()

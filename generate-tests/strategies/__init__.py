"""Strategy registry for compliance prompt generation. Use generator.py --strategy <name>."""
from .base import Strategy
from . import zero_shot
from . import multi_shot
from . import few_shot
from . import iterative
from . import chain_of_thought
from . import prompt_chaining
from . import tree_of_thoughts
from . import self_consistency
from . import self_reflection
from . import directional_stimulus

STRATEGIES = {
    "zero_shot": zero_shot.strategy,
    "multi_shot": multi_shot.strategy,
    "few_shot": few_shot.strategy,
    "iterative": iterative.strategy,
    "chain_of_thought": chain_of_thought.strategy,
    "prompt_chaining": prompt_chaining.strategy,
    "tree_of_thoughts": tree_of_thoughts.strategy,
    "self_consistency": self_consistency.strategy,
    "self_reflection": self_reflection.strategy,
    "directional_stimulus": directional_stimulus.strategy,
}


def get_strategy(name: str) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {name}. Choose from: {list(STRATEGIES.keys())}")
    return STRATEGIES[name]

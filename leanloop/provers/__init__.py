"""Prover backends. The local prover is reached over HTTP, so single-machine
and remote-GPU deployments differ only by `base_url` in config."""
from .base import Prover, ProofAttempt
from .ollama import LocalProver
from .frontier import FrontierProver
from .ensemble import EnsembleProver

__all__ = ["Prover", "ProofAttempt", "LocalProver", "FrontierProver", "EnsembleProver"]

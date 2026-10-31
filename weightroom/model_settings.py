"""Compatibility import for the llama.cpp engine implementation."""
import sys
from engines.llama_cpp import form as implementation

sys.modules[__name__] = implementation

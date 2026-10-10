"""Compatibility import for the channel-independent bridge core."""
import sys
from herdr_bridge import dialog as _implementation
sys.modules[__name__] = _implementation

"""Compatibility import for the channel-independent bridge core."""
import sys
from herdr_bridge import events as _implementation
sys.modules[__name__] = _implementation

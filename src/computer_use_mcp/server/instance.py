"""The shared FastMCP instance and the server logger (one construction; renamed from `mcp` - a submodule may not collide with the external mcp SDK)."""

import logging
import os

from mcp.server.fastmcp import FastMCP

# D2 (Commander, DELTA 1): the FIXED name replaces the moved line 162's __name__, so
# every path (import AND -m, which today logs as "__main__") shares one logger name.

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("computer_use_mcp.server")
mcp = FastMCP("Cortex")

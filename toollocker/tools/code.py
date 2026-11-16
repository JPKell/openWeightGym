"""Compatible code MCP entry point; implementation lives in developer_tools.code."""

import sys
from developer_tools import code as _code

# The tool-folder loader uses the explicit export list; regular imports retain module identity.
TOOLS = _code.TOOLS
setup = _code.setup
if __name__ == "__main__":
    _code.main()
else:
    sys.modules[__name__] = _code

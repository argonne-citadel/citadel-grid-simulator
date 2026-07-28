"""CyDERMS composite model wrapper for the grid-simulator.

Thin wrapper that imports from the common volume (/usr/app/common/cyderms/)
and instantiates CyDERMSCompositeModel. Keeps the model class in the cyderms
library while giving grid-simulator a local model file to reference.

The common volume is mounted read-only in docker-compose.yml:
    ./dockerfiles/console/src:/usr/app/common:ro
"""

import logging
import sys

logger = logging.getLogger(__name__)

# Add the common volume to sys.path so cyderms is importable.
# In Docker, this is /usr/app/common (mounted from console/src).
_COMMON_PATH = "/usr/app/common"
if _COMMON_PATH not in sys.path:
    sys.path.insert(0, _COMMON_PATH)

from cyderms.models.grid_simulator_adapter import CyDERMSCompositeModel  # noqa: E402

__all__ = ["CyDERMSCompositeModel"]

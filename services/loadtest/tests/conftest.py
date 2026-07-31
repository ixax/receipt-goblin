import sys
from pathlib import Path

# Add paths in order: this service first, then services root
_service_dir = str(Path(__file__).resolve().parent.parent)
_services_dir = str(Path(__file__).resolve().parent.parent.parent)

# Rebuild sys.path to ensure the right order without duplicates
_new_path = [_service_dir, _services_dir]
_new_path.extend([p for p in sys.path if p not in (_service_dir, _services_dir)])
sys.path[:] = _new_path

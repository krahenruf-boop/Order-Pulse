import sys
from workstations import health
try:
    for role in ([sys.argv[1]] if len(sys.argv)>1 else ["finance","materials"]):health(role)
except Exception:sys.exit(1)

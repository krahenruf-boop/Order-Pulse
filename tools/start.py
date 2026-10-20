"""Start isolated workstations; optionally pass finance or materials."""
import sys
from workstations import run
if __name__=="__main__":
    roles=[sys.argv[1]] if len(sys.argv)>1 else ["finance","materials"]
    for role in roles:run("start",role)

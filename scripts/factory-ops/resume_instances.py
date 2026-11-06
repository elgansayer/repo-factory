"""Explicit manual-launch helper; preserve unrelated control fields."""
import json
import os
from pathlib import Path
import tempfile
import sys

for name in ('/var/lib/repo-factory/neptune/state/control.json',
             '/var/lib/hellotalk-factory/control.json',
             '/var/lib/repo-factory/workout-agent/state/control.json'):
    path = Path(name)
    data = json.loads(path.read_text()) if path.exists() else {}
    data['paused'] = '--pause' in sys.argv[1:]
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix='.control-')
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(staged, path)

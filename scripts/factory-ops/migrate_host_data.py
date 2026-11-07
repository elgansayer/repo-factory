"""Move only listed factory data directories, verify checksums, preserve paths with bind mounts."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path('/mnt/HC_Volume_106574422/repo-factory/host-data')
ITEMS = [('/home/dev/neptune-evidence', 'neptune-evidence'),
         ('/home/dev/Sega-Saturn-BIOS', 'saturn-reference-assets'),
         ('/home/dev/neptune-asset-source', 'neptune-asset-source'),
         ('/home/dev/factory-tools', 'factory-tools'),
         ('/home/dev/.local/share/godot', 'godot-tools')]

def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout

def unchanged(source, target):
    difference = run('rsync', '-aHAXnc', '--delete', '--out-format=%i %n', str(source)+'/', str(target)+'/')
    if difference.strip():
        raise RuntimeError(f'Copy differs for {source}: {difference[:500]}')

def main():
    if os.geteuid() != 0:
        raise SystemExit('Run with sudo')
    ROOT.mkdir(parents=True, exist_ok=True)
    for source, name in ITEMS:
        source, target = Path(source), ROOT / name
        parked = source.with_name(source.name + '.migration-original-20261106')
        if os.path.ismount(source):
            print(f'Already mounted: {source}', flush=True)
            continue
        if source.is_symlink() or not source.is_dir() or target.exists() or parked.exists():
            raise RuntimeError(f'Unexpected path state: {source}')
        print(f'Copy and checksum: {source}', flush=True)
        target.mkdir()
        run('rsync', '-aHAX', '--numeric-ids', str(source)+'/', str(target)+'/')
        unchanged(source, target)
        source.rename(parked)
        source.mkdir()
        unit = run('systemd-escape', '--path', '--suffix=mount', str(source)).strip()
        unit_source = Path('/home/dev/factory-ops/storage-units') / unit
        run('install', '-m', '644', str(unit_source), '/etc/systemd/system/' + unit)
        run('systemctl', 'daemon-reload')
        try:
            run('systemctl', 'enable', '--now', unit)
            unchanged(parked, source)
        except Exception:
            # Leave the verified original directory intact for recovery.
            raise
        # Remove only this verified duplicate; full data is retained at target.
        assert parked.parent == source.parent and parked.name.endswith('.migration-original-20261106')
        shutil.rmtree(parked)
        print(f'MOVED {source} -> {target}; checksum verified; original path preserved', flush=True)
    print(run('df', '-h', '/', str(ROOT)), flush=True)

if __name__ == '__main__':
    main()

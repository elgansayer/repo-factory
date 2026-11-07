"""Persistent headless Ghidra projects; decompile or rename one exact function."""
import hashlib
import os
from pathlib import Path
from filelock import FileLock


def run(binary: Path, target: str, rename: str | None = None) -> str:
    import pyghidra
    install = os.environ.get('GHIDRA_INSTALL_DIR', '/home/dev/factory-tools/ghidra/ghidra_12.2_PUBLIC')
    if not pyghidra.started():
        pyghidra.start(install_dir=install)
    from ghidra.app.decompiler import DecompInterface
    from ghidra.program.model.symbol import SourceType
    from ghidra.util.task import ConsoleTaskMonitor

    stat = binary.stat()
    identity = f'{binary}:{stat.st_size}:{stat.st_mtime_ns}'
    name = hashlib.sha256(identity.encode()).hexdigest()[:20]
    root = Path(os.environ.get('FACTORY_GHIDRA_PROJECTS', str(Path.cwd() / 'factory-ghidra-projects')))
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / (name + '.lock')), timeout=120):
        with pyghidra.open_program(binary, project_location=root, project_name=name) as api:
            program = api.getCurrentProgram()
            manager = program.getFunctionManager()
            if target.startswith('0x'):
                func = manager.getFunctionAt(api.toAddr(target))
            else:
                matches = [f for f in manager.getFunctions(True)
                           if str(f.getName()) == target or str(f.getName(True)) == target]
                if len(matches) != 1:
                    raise ValueError(f'Expected one exact function for {target!r}; found {len(matches)}. Use an address.')
                func = matches[0]
            if func is None:
                raise ValueError(f'No function at {target}')
            if rename:
                tx = program.startTransaction('Factory symbol rename')
                ok = False
                try:
                    func.setName(rename, SourceType.USER_DEFINED)
                    ok = True
                finally:
                    program.endTransaction(tx, ok)
                # open_program saves the project after closing its own transaction.
                return f'Renamed {target} to {rename} at {func.getEntryPoint()}'
            decompiler = DecompInterface()
            try:
                decompiler.openProgram(program)
                result = decompiler.decompileFunction(func, 60, ConsoleTaskMonitor())
                if not result.decompileCompleted():
                    raise RuntimeError(str(result.getErrorMessage()))
                return str(result.getDecompiledFunction().getC())
            finally:
                decompiler.dispose()

"""Package previously hash-verified Linux wheels into a deterministic regular-file tar."""
import hashlib
import io
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent
output = root / 'wheelhouse'
files = {'requirements.lock.txt': (root / 'requirements.lock.txt').read_bytes()}
for path in sorted((output / 'wheels').glob('*.whl')):
    if path.is_symlink() or not path.is_file():
        raise ValueError('wheel must be a regular file')
    files['wheels/' + path.name] = path.read_bytes()
if len(files) < 2:
    raise ValueError('download locked Linux wheels first')
sha = lambda b: hashlib.sha256(b).hexdigest()
files['SHA256SUMS'] = ''.join(sha(data) + '  ' + name + '\n' for name, data in sorted(files.items())).encode()
dest = output / 'wheelhouse.tar'
with tarfile.open(dest, 'w', format=tarfile.PAX_FORMAT) as tar:
    for name, data in sorted(files.items()):
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        tar.addfile(info, io.BytesIO(data))
receipt = {'tar_path': 'worker-image/wheelhouse/wheelhouse.tar', 'tar_sha256': sha(dest.read_bytes()),
           'tar_bytes': dest.stat().st_size, 'sha256sums_sha256': sha(files['SHA256SUMS']),
           'requirements_sha256': sha(files['requirements.lock.txt'])}
(output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print(json.dumps(receipt, indent=2))

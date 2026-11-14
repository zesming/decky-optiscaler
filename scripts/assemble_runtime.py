#!/usr/bin/env python3
"""Reassemble the pinned runtime from the two official inputs, without running them.

Provide the v0.9.4 release archive and Actions artifact 13898752558's raw 7z.
The exact INI is fetched from the fixed source commit via gh's existing auth.
Archive encoding can vary with the archiver; input file hashes are authoritative.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'py_modules'))
from optiscaler.payload import extract_archive

STABLE_SHA = '575cb4df866116093df75af607e37fd70e10f5163e0f23fd5c804142e80ef0ad'
CORE_SHA = '3835c3a80847bae2a9bb5d873a42e43d7cf504b172650bb54b7659b67e7fdf27'
COMMIT = '6cbd89b887f5f186c294e2fe2b3024692ea21007'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stable-archive', type=Path, required=True)
    parser.add_argument('--core-artifact', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-ini', type=Path, help='Offline fixed-commit INI; hash must match the pinned inventory')
    args = parser.parse_args()
    for path, expected in [(args.stable_archive, STABLE_SHA), (args.core_artifact, CORE_SHA)]:
        if digest(path) != expected:
            raise ValueError(f'Checksum mismatch: {path.name}')
    if args.output.exists():
        raise FileExistsError('Choose a fresh output archive; never append to an existing payload')
    expected = json.loads((ROOT/'defaults/runtime-manifest.json').read_text())
    with tempfile.TemporaryDirectory(prefix='decky-runtime-inputs-') as tmp:
        stage = Path(tmp)/'runtime'; core = Path(tmp)/'core'
        extract_archive(args.stable_archive, stage)
        extract_archive(args.core_artifact, core)
        shutil.copy2(core/'OptiScaler.dll', stage/'OptiScaler.dll')
        if args.reference_ini:
            ini = args.reference_ini.read_bytes()
        else:
            response = subprocess.check_output(['gh','api',f'repos/optiscaler/OptiScaler/contents/OptiScaler.ini?ref={COMMIT}'])
            ini = base64.b64decode(json.loads(response)['content'])
        (stage/'OptiScaler.ini').write_bytes(ini)
        inventory = {p.relative_to(stage).as_posix(): {'bytes':p.stat().st_size,'sha256':digest(p)}
                     for p in stage.rglob('*') if p.is_file()}
        if inventory != expected['files']:
            raise ValueError('Component inventory differs from the reviewed runtime pin')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        archiver = next((x for x in ['7z','7zz','7za'] if shutil.which(x)),None)
        if archiver:
            subprocess.run([archiver,'a','-t7z',str(args.output.resolve()),'.'],cwd=stage,check=True)
        elif shutil.which('bsdtar'):
            subprocess.run(['bsdtar','--format','7zip','-cf',str(args.output.resolve()),'-C',str(stage),'.'],check=True)
        else:
            raise RuntimeError('Need a 7z writer or bsdtar')
    print(json.dumps({'archive':args.output.name,'sha256':digest(args.output),'component_files':len(inventory)}))
    print('If encoding differs, review the matching component inventory before updating PAYLOAD_SHA256; never substitute unverified inputs.')


if __name__ == '__main__':
    main()

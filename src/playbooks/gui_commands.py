"""Shared, toolkit-independent CLI argument construction."""
from pathlib import Path
from .runtime import cli_python

from .paths import ROOT
MODES = ('全流程：ACSM / EPUB → 高清 EPUB', '只准备 EPUB：兑换 / 解密',
         '只下载 Google 原图', '只用本地原图替换', '检查 EPUB 插图')


def build_command(source, volume_id, options):
    mode = options['mode']
    source = Path(source)
    command = [cli_python(), '-u']
    if mode in (MODES[0], MODES[1], MODES[3]):
        if mode == MODES[3] and (source.suffix.lower() != '.epub' or not options['images']):
            raise ValueError('Local replacement requires an EPUB and an original-image folder')
        command += [str(ROOT / 'playbooks_app.py'), 'process', '--input', str(source),
                    '--state', options['state'], '--profile', options['profile'],
                    '--work', options['work'], '--min-gain', options['gain'],
                    '--max-dist', options['distance'], '--pace', options['pace']]
        if volume_id:
            command += ['--id', volume_id]
        if options['output']:
            command += ['--output-dir', options['output']]
        if options['images']:
            command += ['--images', options['images']]
        if mode == MODES[1]:
            command += ['--prepare-only']
        if options['overwrite']:
            command += ['--overwrite']
    else:
        command += [str(ROOT / 'playbooks_hires.py')]
        if mode == MODES[2]:
            if not volume_id:
                raise ValueError('A Google volume ID is required for image-only downloads')
            command += ['fetch', '--id', volume_id, '--profile', options['profile'], '--work', options['work'], '--pace', options['pace']]
        else:
            return command + ['inspect', '--epub', str(source)]
    if mode in (MODES[0], MODES[2]) and options['show']:
        command += ['--show-browser']
    if mode in (MODES[0], MODES[2]) and options['keep']:
        command += ['--keep-browser']
    if mode != MODES[2] and options['dry_run']:
        command += ['--dry-run']
    return command

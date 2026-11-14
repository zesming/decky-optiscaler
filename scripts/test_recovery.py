#!/usr/bin/env python3
"""Offline recovery contracts, using synthetic games and injected filesystem faults."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import shutil
import subprocess
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'py_modules'))
from optiscaler import constants, gameprofiles, installer, live, payload, restorepoint
from optiscaler.inifile import IniFile
from optiscaler.service import OptiScalerService


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*')
            if p.is_file() and restorepoint.RESTORE_DIR not in p.parts}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / 'game'; self.game.mkdir()
        self.source = self.root / 'payload'; self.source.mkdir()
        manifest = json.loads((ROOT / 'defaults/runtime-manifest.json').read_text())
        for name in manifest['files']:
            p = self.source / name; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'synthetic support file')
        dll = installer.DLL_MARKER + b'\0' * installer.MIN_OPTISCALER_SIZE
        (self.source / 'OptiScaler.dll').write_bytes(dll)
        shutil.copy2(ROOT / 'defaults/OptiScaler.reference.ini', self.source / constants.INI_NAME)
        synthetic_pins = {name: {'bytes': (self.source / name).stat().st_size,
                                'sha256': hashlib.sha256((self.source / name).read_bytes()).hexdigest()}
                          for name in manifest['files']}
        inventory_pin = patch.object(payload, '_PINNED_FILES', synthetic_pins)
        inventory_pin.start(); self.addCleanup(inventory_pin.stop)
        self.pin = patch.object(constants, 'RUNTIME_DLL_SHA256', hashlib.sha256(dll).hexdigest())
        self.pin.start(); self.addCleanup(self.pin.stop)
        (self.game / 'dxgi.dll').write_bytes(b'original game dxgi')
        (self.game / 'user.txt').write_bytes(b'unrelated user file')
        self.original = tree(self.game)
        self.service = OptiScalerService(ROOT, self.root / 'settings', self.root / 'runtime',
                                         self.root / 'home', logging.getLogger('recovery'))
        self.service.ensure_payload = AsyncMock(return_value=str(self.source))
        self.service.refresh_wiki_if_stale = lambda: None
        self.service.refresh_page_if_stale = lambda page: None

    def install(self, **kwargs):
        return installer.install(self.game, self.source, **kwargs)

    def test_existing_ini_survives_both_install_modes_and_remove(self):
        original_ini = b'; original comments\r\n[FrameGen]\r\nEnabled=true\r\n'
        for preserve in (True, False):
            with self.subTest(preserve=preserve):
                (self.game / constants.INI_NAME).write_bytes(original_ini)
                self.install(preserve_ini=preserve)
                if preserve:
                    self.assertEqual((self.game / constants.INI_NAME).read_bytes(), original_ini)
                installer.uninstall(self.game)
                self.assertEqual((self.game / constants.INI_NAME).read_bytes(), original_ini)
                self.assertEqual((self.game / 'dxgi.dll').read_bytes(), self.original['dxgi.dll'])
                (self.game / constants.INI_NAME).unlink()

    def test_failed_first_install_restores_every_original(self):
        original = installer.atomic_copy
        failed = False
        def fail(source, destination, *args, **kwargs):
            nonlocal failed
            if Path(destination).name == 'libxess.dll' and not failed:
                failed = True
                raise OSError('injected copy failure')
            return original(source, destination, *args, **kwargs)
        with patch.object(installer, 'atomic_copy', side_effect=fail):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(tree(self.game), self.original)

    def test_failed_proxy_move_can_still_be_removed_without_deleting_game_dll(self):
        self.install()
        before = tree(self.game)
        original = installer.atomic_copy
        failed = False
        def fail(source, destination, *args, **kwargs):
            nonlocal failed
            if Path(destination).name == 'libxess.dll' and not failed:
                failed = True
                raise OSError('injected move failure')
            return original(source, destination, *args, **kwargs)
        with patch.object(installer, 'atomic_copy', side_effect=fail):
            with self.assertRaises(OSError):
                self.install(filename='winmm.dll')
        self.assertEqual(tree(self.game), before)
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)

    def test_incremental_components_remain_owned_through_reinstall(self):
        self.install()
        supplied = self.root / 'supplied'; supplied.mkdir()
        for name in constants.FSR4_SUPPORT_FILES:
            (supplied / name).write_bytes(b'user supplied driver support')
        installer.import_fsr4_files(self.game, supplied)
        patcher = supplied / 'OptiPatcher.asi'; patcher.write_bytes(b'patcher')
        installer.install_plugin_file(self.game, patcher, 'plugins/OptiPatcher.asi')
        self.install()
        manifest = installer.read_manifest(self.game)
        self.assertIn('plugins/OptiPatcher.asi', manifest['files'])
        self.assertIn(constants.FSR4_SUPPORT_FILES[0], manifest['files'])
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)

    def test_service_incremental_patcher_disable_restores_original(self):
        self.install()
        plugins = self.game / 'plugins'; plugins.mkdir(exist_ok=True)
        original = b'pre-existing independent patcher'
        (plugins / 'OptiPatcher.asi').write_bytes(original)
        enabled = asyncio.run(self.service.install_optipatcher(str(self.game), True))
        self.assertTrue(enabled['ok'], enabled)
        disabled = asyncio.run(self.service.install_optipatcher(str(self.game), False))
        self.assertTrue(disabled['ok'], disabled)
        self.assertEqual((plugins / 'OptiPatcher.asi').read_bytes(), original)

    def test_restore_point_and_remove_repeat_without_losing_oldest_originals(self):
        for cycle in range(2):
            with self.subTest(cycle=cycle):
                self.install()
                old = installer.read_manifest(self.game); old['optiscaler_version'] = '0.9.4'
                (self.game / constants.MANIFEST_NAME).write_text(json.dumps(old))
                ini = self.game / constants.INI_NAME
                ini.write_bytes(b'; legacy comments\r\n[FSR]\r\nFsr4ForceEnableInt8=true\r\n')
                before = tree(self.game)
                metadata = {'game_path': '/games/PBZ', 'launch_options': 'LSFG=1 gamescope -- %command%',
                            'game_settings': {'target': None, 'auto': False, 'experiment': None}}
                point = restorepoint.create(self.game, metadata)
                self.assertTrue(point['available'])
                # A repeated request must not replace the first restore point.
                restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': 'different'})
                self.install()
                ini.write_text('[FrameGen]\nEnabled=false\n[FSR]\nFsr4ForceEnableInt8=1\n')
                addon = self.root / 'driver.dll'; addon.write_bytes(b'experiment addon')
                installer.install_plugin_file(self.game, addon, 'plugins/OptiPatcher.asi')
                restorepoint.restore(self.game)
                self.assertEqual(tree(self.game), before)
                self.assertEqual(restorepoint.info(self.game)['metadata'], metadata)
                restorepoint.restore(self.game)  # Retry is safe if Steam's setter failed.
                installer.uninstall(self.game)
                self.assertEqual(tree(self.game), self.original)
                self.assertFalse((self.game / restorepoint.RESTORE_DIR).exists())

    def test_remove_upgraded_install_does_not_restore_old_optiscaler(self):
        self.install()
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': '%command%'})
        self.install(filename='winmm.dll')
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)
        self.assertFalse((self.game / restorepoint.RESTORE_DIR).exists())

    def test_known_old_asi_is_removed_for_upgrade_and_restored_on_rollback(self):
        self.install()
        asi = live.asi_path(self.game); asi.parent.mkdir(exist_ok=True)
        content = b'known legacy ASI bytes'; asi.write_bytes(content)
        old = installer.read_manifest(self.game)
        old['optiscaler_version'] = '0.9.4'; old['live_asi'] = True
        old['files'].append(f'{live.PLUGIN_SUBDIR}/{live.ASI_NAME}')
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps(old))
        before = tree(self.game)
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': 'LSFG=1 %command%'})
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(content).hexdigest()):
            self.install()
        self.assertFalse(asi.exists())
        self.assertFalse(installer.read_manifest(self.game)['live_asi'])
        restorepoint.restore(self.game)
        self.assertEqual(tree(self.game), before)
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)

    def test_unknown_or_unremovable_old_asi_blocks_upgrade_without_file_changes(self):
        self.install()
        asi = live.asi_path(self.game); asi.parent.mkdir(exist_ok=True)
        asi.write_bytes(b'unknown potentially incompatible ASI')
        before = tree(self.game)
        with self.assertRaises((ValueError, RuntimeError)):
            self.install()
        self.assertEqual(tree(self.game), before)
        known = asi.read_bytes(); real_stash = installer._stash; failed = False
        def refuse(target, source, logger=None):
            nonlocal failed
            if Path(source).resolve() == asi.resolve() and not failed:
                failed = True
                raise PermissionError('injected ASI preservation denial')
            return real_stash(target, source, logger)
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(known).hexdigest()), \
                patch.object(installer, '_stash', new=refuse):
            with self.assertRaises(PermissionError):
                self.install()
        self.assertEqual(tree(self.game), before)

    def test_failed_rollback_keeps_current_install_and_can_be_retried(self):
        self.install()
        before = tree(self.game)
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': 'LSFG=1 %command%'})
        self.install(filename='winmm.dll')
        current = tree(self.game)
        copy = installer.atomic_copy; failed = False
        def fail_once(source, destination, *args, **kwargs):
            nonlocal failed
            if Path(destination).name == constants.MANIFEST_NAME and not failed:
                failed = True
                raise OSError('injected rollback commit failure')
            return copy(source, destination, *args, **kwargs)
        with patch.object(installer, 'atomic_copy', side_effect=fail_once):
            with self.assertRaises(OSError):
                restorepoint.restore(self.game)
        self.assertTrue(failed, 'fault did not exercise the rollback commit')
        self.assertEqual(tree(self.game), current)
        self.assertTrue(restorepoint.info(self.game)['available'])
        restorepoint.restore(self.game)
        self.assertEqual(tree(self.game), before)

    def test_service_rollback_restores_only_the_related_game_preferences(self):
        self.install()
        game_path = '/games/PBZ'
        self.service.settings.set_target(game_path, 'old target')
        self.service.settings.set_pref('auto_mode:' + game_path, False)
        self.service.settings.set_pref('auto_mode:/games/other', True)
        point = asyncio.run(self.service.create_restore_point(str(self.game), game_path, 'LSFG=1 %command%'))
        self.assertTrue(point['ok'], point)
        self.service.settings.set_target(game_path, 'new target')
        self.service.settings.set_pref('auto_mode:' + game_path, True)
        self.service.settings.set_pref('deck-experiment:' + game_path, True)
        rolled = asyncio.run(self.service.rollback_install(str(self.game), game_path))
        self.assertTrue(rolled['ok'], rolled)
        self.assertEqual(rolled['launch_options'], 'LSFG=1 %command%')
        self.assertEqual(self.service.settings.snapshot_game(game_path),
                         {'target': 'old target', 'auto': False, 'experiment': None})
        self.assertTrue(self.service.settings.get_pref('auto_mode:/games/other'))

    def test_snapshot_restores_originals_first_adopted_after_snapshot(self):
        (self.game / 'winmm.dll').write_bytes(b'original game winmm')
        support_name = constants.FSR4_SUPPORT_FILES[0]
        (self.game / support_name).write_bytes(b'original AMD support')
        self.install()
        before = tree(self.game)
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''})
        self.install(filename='winmm.dll')
        supplied = self.root / 'support'; supplied.mkdir()
        for name in constants.FSR4_SUPPORT_FILES:
            (supplied / name).write_bytes(b'experimental AMD support')
        installer.import_fsr4_files(self.game, supplied)
        for _ in range(2):
            restorepoint.restore(self.game)
            self.assertEqual(tree(self.game), before)
        installer.uninstall(self.game)
        self.assertEqual((self.game / 'winmm.dll').read_bytes(), b'original game winmm')
        self.assertEqual((self.game / support_name).read_bytes(), b'original AMD support')

    def test_switching_proxy_keeps_manual_optiscaler_stashed_until_remove(self):
        manual = installer.DLL_MARKER + b'legacy manual core' + b'\0' * installer.MIN_OPTISCALER_SIZE
        (self.game / 'dxgi.dll').write_bytes(manual)
        self.install()
        self.install(filename='winmm.dll')
        self.assertFalse((self.game / 'dxgi.dll').exists(), 'old core would be loaded alongside the new proxy')
        installer.uninstall(self.game)
        self.assertEqual((self.game / 'dxgi.dll').read_bytes(), manual)

    def test_reinstall_rejects_nested_destination_symlink(self):
        self.install()
        outside = self.root / 'outside.dll'; outside.write_bytes(b'outside sentinel')
        nested = self.game / 'D3D12_Optiscaler/D3D12Core.dll'
        nested.unlink(); nested.symlink_to(outside)
        with self.assertRaises((ValueError, RuntimeError)):
            self.install()
        self.assertEqual(outside.read_bytes(), b'outside sentinel')
        self.assertTrue(nested.is_symlink())

    def test_remove_rejects_file_replaced_with_nonempty_directory(self):
        self.install()
        path = self.game / 'libxess.dll'; path.unlink(); path.mkdir()
        (path / 'user-data').write_bytes(b'keep')
        with self.assertRaises((ValueError, RuntimeError)):
            installer.uninstall(self.game)
        self.assertEqual((path / 'user-data').read_bytes(), b'keep')

    def test_disabling_unowned_component_is_idempotent_and_missing_backup_blocks(self):
        self.install()
        plugins = self.game / 'plugins'; plugins.mkdir(exist_ok=True)
        original = plugins / 'OptiPatcher.asi'; original.write_bytes(b'user patcher')
        for _ in range(2):
            installer.remove_files(self.game, ['plugins/OptiPatcher.asi'])
            self.assertEqual(original.read_bytes(), b'user patcher')
        source = self.root / 'patcher'; source.write_bytes(b'managed patcher')
        installer.install_plugin_file(self.game, source)
        manifest = installer.read_manifest(self.game)
        stored = manifest['backups']['plugins/OptiPatcher.asi']
        (installer.backup_dir(self.game) / stored).unlink()
        before = tree(self.game)
        with self.assertRaises((ValueError, RuntimeError, FileNotFoundError)):
            installer.remove_files(self.game, ['plugins/OptiPatcher.asi'])
        self.assertEqual(tree(self.game), before)

    def test_manual_install_restores_preferences_and_launch_record_atomically(self):
        game = '/games/manual'
        self.service.settings.set_target(game, 'original-target')
        self.service.settings.set_pref('auto_mode:' + game, True)
        result = asyncio.run(self.service.install(str(self.game), game_path=game,
                            launch_options='LSFG=1 %command%', auto_mode=False))
        self.assertTrue(result['ok'], result)
        self.assertFalse(self.service.settings.get_pref('auto_mode:' + game, False))
        self.assertEqual(installer.read_launch_record(self.game)['value'], 'LSFG=1 %command%')
        result = asyncio.run(self.service.uninstall(str(self.game)))
        self.assertTrue(result['ok'], result)
        self.assertEqual(tree(self.game), self.original)
        self.assertEqual(self.service.settings.get_target(game), 'original-target')
        self.assertTrue(self.service.settings.get_pref('auto_mode:' + game))

    def test_failed_first_service_install_leaves_no_launch_record_or_preferences(self):
        game = '/games/failed'
        with patch.object(self.service.settings, 'configure_game', side_effect=OSError('settings disk failure')):
            result = asyncio.run(self.service.install(str(self.game), game_path=game,
                                launch_options='LSFG=1 %command%', auto_mode=False))
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), self.original)
        self.assertEqual(self.service.settings.snapshot_game(game), {'target': None, 'auto': None, 'experiment': None})

    def test_actual_old_runtime_requires_complete_prepared_snapshot(self):
        self.install()
        legacy = installer.DLL_MARKER + b'older core' + b'\0' * installer.MIN_OPTISCALER_SIZE
        (self.game / 'dxgi.dll').write_bytes(legacy)
        self.assertFalse(installer.runtime_current(self.game))
        before = tree(self.game)
        result = asyncio.run(self.service.install(str(self.game)))
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), before)
        point = asyncio.run(self.service.create_restore_point(str(self.game), '/games/PBZ', 'LSFG=1 %command%'))
        self.assertTrue(point['ok'], point)
        result = asyncio.run(self.service.install(str(self.game)))
        self.assertTrue(result['ok'], result)
        rolled = asyncio.run(self.service.rollback_install(str(self.game), '/games/PBZ'))
        self.assertTrue(rolled['ok'], rolled)
        self.assertEqual((self.game / 'dxgi.dll').read_bytes(), legacy)

    def test_interrupted_transaction_recovers_after_process_restart(self):
        ini = self.game / constants.INI_NAME
        ini.write_bytes(b'original before abrupt exit')
        ini.chmod(0o640)
        code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from optiscaler import installer
with installer.target_transaction(sys.argv[2], include_known=False):
 Path(sys.argv[2], 'OptiScaler.ini').write_bytes(b'interrupted partial mutation')
 os._exit(73)
"""
        result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'py_modules'), str(self.game)], capture_output=True)
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        self.assertEqual(ini.read_bytes(), b'interrupted partial mutation')
        recovered = installer.recover_target_transactions(self.game)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(ini.read_bytes(), b'original before abrupt exit')
        self.assertEqual(ini.stat().st_mode & 0o777, 0o640)
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))

    def test_launch_record_failed_publish_can_retry_and_unreadable_is_rejected(self):
        with patch.object(os, 'link', side_effect=OSError('publish denied')):
            with self.assertRaises(OSError):
                installer.record_launch_options(self.game, 'before')
        self.assertFalse(installer.read_launch_record(self.game)['recorded'])
        self.assertTrue(installer.record_launch_options(self.game, 'before')['written'])
        self.assertEqual(installer.record_launch_options(self.game, 'later')['value'], 'before')
        (self.game / constants.LAUNCH_RECORD_NAME).write_bytes(b'\xff')
        with self.assertRaises(ValueError):
            installer.record_launch_options(self.game, 'must not erase unreadable record')

    def test_custom_asi_paths_and_renamed_legacy_asi_block_upgrade(self):
        self.install()
        ini = self.game / constants.INI_NAME
        original = ini.read_bytes()
        for text in ('[Plugins]\nPath=default\n', '[Libraries]\nOptiDllPath=custom\n'):
            ini.write_text(text)
            before = tree(self.game)
            with self.assertRaises((ValueError, RuntimeError)):
                self.install()
            self.assertEqual(tree(self.game), before)
        ini.write_bytes(original)
        plugins = self.game / 'plugins'; plugins.mkdir(exist_ok=True)
        renamed = plugins / 'decky_optiscaler_live-loadlate.ASI'
        renamed.write_bytes(b'known renamed legacy')
        before = tree(self.game)
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(renamed.read_bytes()).hexdigest()):
            with self.assertRaises((ValueError, RuntimeError)):
                self.install()
        self.assertEqual(tree(self.game), before)

    def test_guard_change_preserves_journal_for_later_retry(self):
        ini = self.game / constants.INI_NAME; ini.write_bytes(b'original')
        running = False
        def stopped(_):
            if running:
                raise RuntimeError('game started during mutation')
        with patch.object(installer, 'require_stopped_target', side_effect=stopped):
            with self.assertRaises(RuntimeError):
                with installer.target_transaction(self.game, include_known=False, require_stopped=True):
                    ini.write_bytes(b'partial mutation')
                    running = True
                    raise OSError('injected operation error')
            self.assertTrue(list(self.game.glob('.decky-optiscaler-tx-*')))
            running = False
            installer.recover_target_transactions(self.game)
        self.assertEqual(ini.read_bytes(), b'original')
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))

    def test_snapshot_preparation_failure_never_rewrites_target(self):
        ini = self.game / constants.INI_NAME; ini.write_bytes(b'original ini')
        before = tree(self.game)
        identities = {p.name: p.stat().st_ino for p in (ini, self.game / 'dxgi.dll')}
        original_copy = installer._copy_path; count = 0
        def fail_second(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('snapshot disk failure')
            return original_copy(source, destination)
        with patch.object(installer, '_copy_path', side_effect=fail_second):
            with self.assertRaises(OSError):
                with installer.target_transaction(self.game):
                    self.fail('mutation ran before snapshot completed')
        self.assertEqual(tree(self.game), before)
        for name, inode in identities.items():
            self.assertEqual((self.game / name).stat().st_ino, inode)
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))

    def test_original_empty_directories_and_permissions_survive_remove(self):
        plugins = self.game / 'plugins'; plugins.mkdir(mode=0o750)
        licenses = self.game / 'Licenses'; licenses.mkdir(mode=0o700)
        ini = self.game / constants.INI_NAME; ini.write_bytes(b'; original ini')
        ini.chmod(0o640); (self.game / 'dxgi.dll').chmod(0o600)
        self.install()
        installer.uninstall(self.game)
        self.assertTrue(plugins.is_dir()); self.assertEqual(list(plugins.iterdir()), [])
        self.assertTrue(licenses.is_dir()); self.assertEqual(list(licenses.iterdir()), [])
        for path, expected in ((plugins,0o750),(licenses,0o700),(ini,0o640),(self.game/'dxgi.dll',0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, expected, str(path))

    def test_rollback_preserves_untouched_candidate_user_changes(self):
        plugins = self.game / 'plugins'; plugins.mkdir()
        user = plugins / 'OptiPatcher.asi'; user.write_bytes(b'user original')
        self.install()
        restorepoint.create(self.game, {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''})
        user.write_bytes(b'user changed while experiment was installed')
        self.install(filename='winmm.dll')
        restorepoint.restore(self.game)
        self.assertEqual(user.read_bytes(), b'user changed while experiment was installed')
        installer.uninstall(self.game)
        self.assertEqual(user.read_bytes(), b'user changed while experiment was installed')

    def test_settings_persist_failure_reverts_install_and_both_game_preferences(self):
        game = '/games/persist-failure'
        self.service.settings.set_target(game, 'old target')
        self.service.settings.set_pref('auto_mode:' + game, True)
        original_settings = self.service.settings.path.read_bytes()
        replace = Path.replace; failed = False
        def refuse(path, destination):
            nonlocal failed
            if Path(destination) == self.service.settings.path and not failed:
                failed = True
                raise OSError('injected settings commit failure')
            return replace(path, destination)
        with patch.object(Path, 'replace', new=refuse):
            result = asyncio.run(self.service.install(str(self.game), game_path=game,
                                launch_options='LSFG=1 %command%', auto_mode=False))
        self.assertTrue(failed)
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), self.original)
        self.assertEqual(self.service.settings.get_target(game), 'old target')
        self.assertTrue(self.service.settings.get_pref('auto_mode:' + game))
        self.assertEqual(self.service.settings.path.read_bytes(), original_settings)

    def test_existing_runtime_configuration_stays_in_place_until_remove(self):
        original = b'; user nvngx configuration'
        path = self.game / 'nvngx.ini'; path.write_bytes(original)
        self.install()
        self.assertEqual(path.read_bytes(), original)
        path.write_bytes(b'; changed by game runtime')
        self.install()
        self.assertEqual(path.read_bytes(), b'; changed by game runtime')
        installer.uninstall(self.game)
        self.assertEqual(path.read_bytes(), original)

    def test_null_metadata_or_missing_snapshot_entry_blocks_upgrade(self):
        self.install()
        with self.assertRaises(ValueError):
            restorepoint.create(self.game, {'game_path': None, 'game_settings': None, 'launch_options': None})
        metadata = {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''}
        restorepoint.create(self.game, metadata)
        path = self.game / restorepoint.RESTORE_DIR / restorepoint.SNAPSHOT_FILE
        record = json.loads(path.read_text()); record['entries'].pop()
        path.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            restorepoint.info(self.game)

    def test_unmanaged_incremental_installs_are_refused_without_writes(self):
        source = self.root / 'unmanaged-support'; source.mkdir()
        for name in constants.FSR4_SUPPORT_FILES:
            (source / name).write_bytes(b'new support')
        before = tree(self.game)
        with self.assertRaises((ValueError, RuntimeError)):
            installer.import_fsr4_files(self.game, source)
        self.assertEqual(tree(self.game), before)
        reframework = source / constants.REFRAMEWORK_DLL; reframework.write_bytes(b'new REF')
        with self.assertRaises((ValueError, RuntimeError)):
            installer.add_files(self.game, [reframework])
        self.assertEqual(tree(self.game), before)

    def test_rollback_preserves_unowned_files_inside_payload_directories(self):
        self.install()
        licenses = self.game / 'Licenses'
        untouched = licenses / 'user.txt'; untouched.write_bytes(b'before')
        metadata = {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''}
        restorepoint.create(self.game, metadata)
        untouched.write_bytes(b'edited by user')
        added = licenses / 'new-user.txt'; added.write_bytes(b'new user data')
        self.install(filename='winmm.dll')
        restorepoint.restore(self.game)
        self.assertEqual(untouched.read_bytes(), b'edited by user')
        self.assertEqual(added.read_bytes(), b'new user data')

    def test_casefolded_ini_and_plugin_directory_aliases_cannot_load_old_asi(self):
        self.install()
        ini = self.game / constants.INI_NAME; original = ini.read_bytes()
        for text in ('[plugins]\npath=custom\n', '\ufeff[libraries]\noptidllpath=custom\n'):
            ini.write_text(text)
            before = tree(self.game)
            result = asyncio.run(self.service.write_config(str(self.game), [
                {'section': 'FrameGen', 'key': 'Enabled', 'value': 'false'}]))
            self.assertFalse(result['ok'], result)
            self.assertEqual(tree(self.game), before)
        ini.write_bytes(original)
        aliases = self.game / 'Plugins'; aliases.mkdir()
        asi = aliases / live.ASI_NAME; asi.write_bytes(b'legacy bytes')
        before = tree(self.game)
        with patch.object(constants, 'LEGACY_LIVE_ASI_SHA256', hashlib.sha256(asi.read_bytes()).hexdigest()):
            with self.assertRaises((ValueError, RuntimeError)):
                self.install()
        self.assertEqual(tree(self.game), before)

    def test_final_proposed_asi_configuration_is_rejected_before_ini_commit(self):
        self.install()
        before = tree(self.game)
        result = asyncio.run(self.service.write_config(str(self.game), [
            {'section': 'Plugins', 'key': 'Path', 'value': 'custom'}]))
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), before)

    def test_legacy_suffix_backup_is_restored_and_journaled_on_tail_failure(self):
        legacy = self.game / ('dxgi.dll' + installer.LEGACY_BACKUP_SUFFIX)
        legacy.write_bytes(b'oldest legacy original')
        (self.game / 'dxgi.dll').write_bytes(installer.DLL_MARKER + b'\0' * installer.MIN_OPTISCALER_SIZE)
        before = tree(self.game)
        persist = installer._persist_transaction; failed = False
        def refuse(state, target, status='active'):
            nonlocal failed
            if status == 'committed' and not failed:
                failed = True
                raise OSError('injected transaction tail failure')
            return persist(state, target, status)
        with patch.object(installer, '_persist_transaction', side_effect=refuse):
            with self.assertRaises(RuntimeError):
                installer.uninstall(self.game)
        self.assertTrue(failed)
        installer.recover_target_transactions(self.game)
        self.assertEqual(tree(self.game), before)
        installer.uninstall(self.game)
        self.assertEqual((self.game / 'dxgi.dll').read_bytes(), b'oldest legacy original')
        self.assertFalse(legacy.exists())

    def test_waiting_automatic_operations_observe_committed_experiment_mode(self):
        self.install()
        game = '/games/serialized'
        async def verify():
            for operation in ('apply', 'install'):
                self.service.settings.set_pref('deck-experiment:' + game, False)
                ini = IniFile(self.game / constants.INI_NAME)
                ini.set('FrameGen', 'FGInput', 'nofg'); ini.set('FrameGen', 'FGOutput', 'nofg'); ini.save()
                planned = asyncio.Event()
                async def plan(*args):
                    planned.set()
                    return {'recommendation': {}, 'plan': {'available': True, 'filename': 'dxgi.dll',
                        'optipatcher': False, 'settings': [], 'framegen': {'input': 'fsrfg', 'output': 'fsr3'}}}
                self.service.get_auto_plan = AsyncMock(side_effect=plan)
                await self.service._mutation_lock.acquire()
                try:
                    coro = (self.service.apply_auto_settings(str(self.game), game, 'Test') if operation == 'apply'
                            else self.service.auto_install(str(self.game), game, 'Test'))
                    task = asyncio.create_task(coro)
                    await planned.wait()
                    await asyncio.sleep(0)
                    self.service.settings.set_pref('deck-experiment:' + game, True)
                finally:
                    self.service._mutation_lock.release()
                result = await task
                self.assertTrue(result['ok'], result)
                values = IniFile(self.game / constants.INI_NAME)
                self.assertEqual(values.get('FrameGen', 'FGInput'), 'nofg', operation)
                self.assertEqual(values.get('FrameGen', 'FGOutput'), 'nofg', operation)
        asyncio.run(verify())

    def test_interrupted_committed_journal_cleanup_never_blocks_next_operation(self):
        ini = self.game / constants.INI_NAME; ini.write_bytes(b'old value')
        code = """import os,sys,shutil
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from optiscaler import installer
original=shutil.rmtree
def interrupt(path,*args,**kwargs):
 path=Path(path)
 if path.name.startswith('.decky-optiscaler-tx-'):
  (path/installer.TRANSACTION_RECORD).unlink(missing_ok=True)
  os._exit(74)
 return original(path,*args,**kwargs)
shutil.rmtree=interrupt
with installer.target_transaction(sys.argv[2],include_known=False):
 Path(sys.argv[2],'OptiScaler.ini').write_bytes(b'committed new value')
"""
        result = subprocess.run([sys.executable, '-c', code, str(ROOT/'py_modules'), str(self.game)], capture_output=True)
        self.assertEqual(result.returncode,74,result.stderr.decode())
        installer.recover_target_transactions(self.game)
        self.assertEqual(ini.read_bytes(), b'committed new value')
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))
        with installer.target_transaction(self.game, include_known=False):
            ini.write_bytes(b'next operation')
        self.assertEqual(ini.read_bytes(), b'next operation')

    def test_unmanaged_known_runtime_cannot_change_unrecoverable_configuration(self):
        shutil.copy2(self.source / 'OptiScaler.dll', self.game / 'dxgi.dll')
        shutil.copy2(self.source / constants.INI_NAME, self.game / constants.INI_NAME)
        (self.game / constants.LOG_NAME).write_bytes(b'original manual log')
        before = tree(self.game)
        self.assertTrue(installer.runtime_current(self.game))
        self.assertFalse(asyncio.run(self.service.read_config(str(self.game)))['runtime_current'])
        actions = [self.service.write_config(str(self.game), [{'section':'FrameGen','key':'Enabled','value':'false'}]),
                   self.service.reset_config(str(self.game)),
                   self.service.record_launch_options(str(self.game), 'LSFG=1 %command%'),
                   self.service.clear_log(str(self.game))]
        for action in actions:
            result = asyncio.run(action)
            self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), before)

    def test_clear_log_refuses_symlink_and_preserves_outside_file(self):
        self.install()
        outside = self.root / 'outside-log'; outside.write_bytes(b'outside log data')
        log = self.game / constants.LOG_NAME; log.symlink_to(outside)
        result = asyncio.run(self.service.clear_log(str(self.game)))
        self.assertFalse(result['ok'], result)
        self.assertEqual(outside.read_bytes(), b'outside log data')
        self.assertTrue(log.is_symlink())

    def test_first_install_keeps_unowned_directory_children_and_later_edits(self):
        licenses = self.game / 'Licenses'; licenses.mkdir(mode=0o700)
        user = licenses / 'game-license.txt'; user.write_bytes(b'game original license')
        self.install()
        self.assertEqual(user.read_bytes(), b'game original license')
        user.write_bytes(b'game updated license')
        installer.uninstall(self.game)
        self.assertEqual(user.read_bytes(), b'game updated license')
        self.assertEqual(licenses.stat().st_mode & 0o777, 0o700)

    def test_original_added_after_snapshot_is_returned_before_old_backup_restored(self):
        self.install()
        metadata = {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''}
        restorepoint.create(self.game, metadata)
        name = constants.FSR4_SUPPORT_FILES[0]
        target = self.game / name; target.write_bytes(b'new original added by game update')
        source = self.root / 'late-support'; source.mkdir()
        for item in constants.FSR4_SUPPORT_FILES:
            (source / item).write_bytes(b'experimental replacement')
        installer.import_fsr4_files(self.game, source)
        for _ in range(2):
            restorepoint.restore(self.game)
            self.assertEqual(target.read_bytes(), b'new original added by game update')
        installer.uninstall(self.game)
        self.assertEqual(target.read_bytes(), b'new original added by game update')

    def test_rejected_root_proxy_symlink_never_leaves_an_active_journal(self):
        path = self.game / 'dxgi.dll'; path.unlink(); path.symlink_to('user.txt')
        with self.assertRaises((ValueError, RuntimeError)):
            self.install()
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.readlink(), Path('user.txt'))
        self.assertFalse(list(self.game.glob('.decky-optiscaler-tx-*')))
        installer.recover_target_transactions(self.game)
        path.unlink(); path.write_bytes(self.original['dxgi.dll'])
        self.install()
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)

    def test_real_legacy_manifest_directory_ownership_does_not_backup_its_own_payload(self):
        self.install()
        manifest = installer.read_manifest(self.game)
        manifest['files'] = [name for name in manifest['files'] if '/' not in name]
        manifest.pop('original_directories', None); manifest.pop('created_directories', None)
        manifest['optiscaler_version'] = '0.9.4'
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps(manifest))
        (self.game / 'dxgi.dll').write_bytes(installer.DLL_MARKER + b'old runtime' + b'\0' * installer.MIN_OPTISCALER_SIZE)
        metadata = {'game_path': '/games/test', 'game_settings': {'target': None, 'auto': None, 'experiment': None}, 'launch_options': ''}
        restorepoint.create(self.game, metadata)
        self.install()
        current = installer.read_manifest(self.game)
        self.assertFalse(any('/' in name for name in current['backups']))
        installer.uninstall(self.game)
        self.assertEqual(tree(self.game), self.original)
        self.assertFalse((self.game / 'Licenses').exists())
        self.assertFalse((self.game / 'D3D12_Optiscaler').exists())

    def test_preexisting_pinned_directory_file_is_restored_without_touching_other_files(self):
        licenses = self.game / 'Licenses'; licenses.mkdir(mode=0o750)
        pinned = licenses / 'XeSS_LICENSE.txt'; pinned.write_bytes(b'game license original')
        unrelated = licenses / 'game-owned.txt'; unrelated.write_bytes(b'game data')
        self.install()
        self.assertNotEqual(pinned.read_bytes(), b'game license original')
        self.assertEqual(unrelated.read_bytes(), b'game data')
        unrelated.write_bytes(b'game updated data')
        installer.uninstall(self.game)
        self.assertEqual(pinned.read_bytes(), b'game license original')
        self.assertEqual(unrelated.read_bytes(), b'game updated data')
        self.assertEqual(licenses.stat().st_mode & 0o777, 0o750)

    def test_running_target_is_rejected_before_mutation(self):
        before = tree(self.game)
        with patch.object(live, 'status', return_value={'attached': True}):
            with self.assertRaises(RuntimeError):
                self.install()
        self.assertEqual(tree(self.game), before)

    def test_manifest_paths_cannot_delete_a_sibling_or_follow_symlinks(self):
        sentinel = self.root / 'sentinel'; sentinel.write_bytes(b'keep me')
        for name in ('../sentinel', str(sentinel)):
            with self.subTest(name=name):
                (self.game / constants.MANIFEST_NAME).write_text(json.dumps({
                    'plugin': 'decky-optiscaler', 'files': [name], 'dirs': [], 'backups': {}}))
                with self.assertRaises(ValueError):
                    installer.uninstall(self.game)
                self.assertEqual(sentinel.read_bytes(), b'keep me')
        (self.game / constants.MANIFEST_NAME).unlink()
        link = self.game / 'plugins'; link.symlink_to(self.root, target_is_directory=True)
        (self.game / constants.MANIFEST_NAME).write_text(json.dumps({
            'plugin': 'decky-optiscaler', 'files': ['plugins/sentinel'], 'dirs': [], 'backups': {}}))
        with self.assertRaises(ValueError):
            installer.uninstall(self.game)
        self.assertEqual(sentinel.read_bytes(), b'keep me')
        link.unlink()

    def test_automatic_write_failure_is_not_reported_as_success(self):
        planned = {'recommendation': {}, 'plan': {'available': True, 'filename': 'dxgi.dll',
                   'optipatcher': False, 'settings': [{'section': 'Hotfix', 'key': 'DisableOverlays', 'value': 'true'}]}}
        self.service.get_auto_plan = AsyncMock(return_value=planned)
        with patch.object(self.service, '_write_config_sync', return_value={'ok': False, 'error': 'injected INI failure'}):
            result = asyncio.run(self.service.auto_install(str(self.game), '/games/PBZ', 'Phantom Blade Zero'))
        self.assertFalse(result['ok'], result)
        self.assertEqual(tree(self.game), self.original)
        self.assertFalse(self.service.settings.get_pref('auto_mode:/games/PBZ', False))
        self.install()
        with patch.object(self.service, '_write_config_sync', return_value={'ok': False, 'error': 'injected INI failure'}):
            result = asyncio.run(self.service.apply_auto_settings(str(self.game), '/games/PBZ', 'Phantom Blade Zero'))
        self.assertFalse(result['ok'], result)
        self.assertFalse(self.service.settings.get_pref('auto_mode:/games/PBZ', False))

    def test_runtime_identity_not_manifest_label_controls_new_schema(self):
        self.install()
        self.assertTrue(installer.runtime_current(self.game))
        (self.game / 'dxgi.dll').write_bytes(b'unknown runtime')
        result = asyncio.run(self.service.write_config(str(self.game), [
            {'section': 'FSR', 'key': 'Fsr4ForceEnableInt8', 'value': '1'}]))
        self.assertFalse(result['ok'], result)

    def test_legacy_boolean_migration_changes_only_the_int8_value(self):
        for old, new in [('true', '1'), ('false', '0'), ('auto', 'auto'), ('2', '2')]:
            with self.subTest(value=old):
                text = f'; keep comment\n[FSR]\nFsr4ForceEnableInt8={old}\n[Custom]\nUnrelated=keep\n'
                (self.game / constants.INI_NAME).write_text(text)
                result = asyncio.run(self.service.install(str(self.game)))
                self.assertTrue(result['ok'], result)
                after = (self.game / constants.INI_NAME).read_text()
                self.assertIn('; keep comment', after)
                self.assertIn('Unrelated=keep', after)
                self.assertEqual(IniFile(self.game / constants.INI_NAME).get('FSR', 'Fsr4ForceEnableInt8'), new)
                installer.uninstall(self.game)
                self.assertEqual((self.game / constants.INI_NAME).read_text(), text)
                (self.game / constants.INI_NAME).unlink()

    def test_beta_never_installs_or_sends_commands_to_old_live_asi(self):
        self.install()
        for call in (lambda: self.service.install_live(str(self.game)),
                     lambda: self.service.switch_upscaler(str(self.game), 'fsr31')):
            self.assertFalse(asyncio.run(call())['ok'])
        status = asyncio.run(self.service.get_live_status(str(self.game)))
        self.assertFalse(status['attached'])
        self.assertFalse(status['supported'])
        self.assertFalse((self.game / live.CMD_FILE).exists())
        self.assertFalse(live.asi_path(self.game).exists())

    def test_pbz_target_and_offline_requirements_are_explicit(self):
        root = self.root / 'PBZ'; canonical = root / 'platform/steam'
        canonical.mkdir(parents=True); (canonical / 'PBZ.exe').write_bytes(b'MZ')
        decoy = root / 'ProjectPBZ/Binaries/Win64'; decoy.mkdir(parents=True)
        (decoy / 'PBZ-Win64-Shipping.exe').write_bytes(b'MZ' + b'\0' * 1024)
        detail = asyncio.run(self.service.get_game(str(root), 'Phantom Blade Zero'))
        self.assertEqual(detail['target'], str(canonical))
        recommendation = gameprofiles.recommendation({'matched': False}, '影之刃零')
        plan = gameprofiles.constrain({}, recommendation)
        self.assertTrue(plan['runtime_supported'])
        self.assertTrue(any('100%' in step for step in plan['manual_steps']))
        with patch.object(gameprofiles, 'OPTISCALER_VERSION', '0.9.4'):
            self.assertFalse(gameprofiles.constrain({}, recommendation)['runtime_supported'])

    def test_pinned_pbz_identity_resolves_renamed_root_for_detail_and_automatic_setup(self):
        root = self.root / 'My game'
        canonical = root / 'platform/steam'
        canonical.mkdir(parents=True)
        (canonical / 'PBZ.exe').write_bytes(b'MZ')
        # This higher ranked candidate has no OptiScaler install. A PBZ pin
        # should make the canonical folder the initial setup target.
        decoy = root / 'OtherGame/Binaries/Win64'
        decoy.mkdir(parents=True)
        (decoy / 'OtherGame-Win64-Shipping.exe').write_bytes(b'MZ')
        self.service.settings.set_wiki_entry(str(root), 'Phantom Blade Zero')

        recommendation = {'matched': True, 'game': 'Phantom Blade Zero',
                          'filename': 'dxgi.dll', 'filename_source': 'wiki entry',
                          'detail': {}, 'compatibility': '✅'}
        base_plan = {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                     'settings': []}
        original_detect = installer.detect

        def detect_with_decoy_install(path):
            if Path(path) == decoy:
                return {'path': str(decoy), 'installed': True, 'filename': 'dxgi.dll'}
            return original_detect(path)

        detail = asyncio.run(self.service.get_game(str(root), 'My Steam shortcut'))
        with patch.object(installer, 'detect', side_effect=detect_with_decoy_install):
            existing_wrong_folder = asyncio.run(
                self.service.get_game(str(root), 'My Steam shortcut'))
        with patch.object(self.service.wiki, 'entry_by_name', return_value={'name': 'Phantom Blade Zero'}), \
             patch.object(self.service.wiki, 'recommend_entry', return_value=recommendation), \
             patch('optiscaler.service.autoplan.build', return_value=base_plan):
            planned = asyncio.run(self.service.get_auto_plan('My Steam shortcut', game_path=str(root)))

        self.assertEqual(detail['pbz_target'], str(canonical))
        self.assertEqual(detail['target'], str(canonical))
        self.assertTrue(detail['pbz_identity'])
        self.assertEqual(existing_wrong_folder['pbz_target'], str(canonical))
        self.assertEqual(existing_wrong_folder['target'], str(decoy))
        self.assertEqual(planned['recommendation']['game'], 'Phantom Blade Zero')
        self.assertEqual(planned['plan']['required_target'], 'PBZ/platform/steam')

        self.service.get_auto_plan = AsyncMock(return_value=planned)
        self.service.install = AsyncMock(return_value={'ok': True, 'applied': []})
        wrong_target = self.root / 'wrong-target'; wrong_target.mkdir()
        rejected_install = asyncio.run(self.service.auto_install(
            str(wrong_target), str(root), 'My Steam shortcut'))
        self.assertFalse(rejected_install['ok'])
        self.service.install.assert_not_awaited()

        installed = asyncio.run(self.service.auto_install(
            str(canonical), str(root), 'My Steam shortcut'))
        self.assertTrue(installed['ok'], installed)
        self.service.install.assert_awaited_once()

        self.service._write_config_sync = lambda target, changes: {
            'ok': True, 'applied': [], 'rejected': []}
        with patch('optiscaler.service.autoplan.setting_changes', return_value=[]), \
             patch('optiscaler.service.autoplan.framegen_changes', return_value=[]):
            rejected_settings = asyncio.run(self.service.apply_auto_settings(
                str(wrong_target), str(root), 'My Steam shortcut'))
            self.assertFalse(rejected_settings['ok'])
            applied_settings = asyncio.run(self.service.apply_auto_settings(
                str(canonical), str(root), 'My Steam shortcut'))
        self.assertTrue(applied_settings['ok'], applied_settings)

    def test_pbz_root_basename_can_supply_recommendation_identity(self):
        root = self.root / 'PBZ'
        canonical = root / 'platform/steam'
        canonical.mkdir(parents=True)
        (canonical / 'PBZ.exe').write_bytes(b'MZ')
        empty = {'matched': False, 'searched': [], 'entry_count': 0,
                 'list_available': False, 'near_misses': [], 'game': None,
                 'page': None, 'filename': 'dxgi.dll', 'filename_source': 'default',
                 'alternatives': [], 'compatibility': None, 'inputs': None,
                 'notes': None, 'optipatcher': False, 'wiki_url': None,
                 'detail': {}, 'detail_pending': False, 'list_meta': {},
                 'match_score': None}
        empty.update(matched=True, game='Unrelated Game', filename='winmm.dll',
                     alternatives=['version.dll'], optipatcher=True,
                     detail={'Settings': 'UnrelatedSetting=true'})
        base_plan = {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                     'settings': []}
        with patch.object(self.service.wiki, 'recommend', return_value=empty), \
             patch('optiscaler.service.autoplan.build', return_value=base_plan):
            planned = asyncio.run(self.service.get_auto_plan(
                'My renamed shortcut', game_path=str(root)))
        self.assertTrue(planned['recommendation']['required_target'])
        self.assertEqual(planned['plan']['required_target'], 'PBZ/platform/steam')
        self.assertEqual(planned['recommendation']['alternatives'], [])
        self.assertFalse(planned['recommendation']['optipatcher'])
        self.assertNotIn('Settings', planned['recommendation']['detail'])

    def test_pbz_identity_requires_an_exact_known_title(self):
        root = self.root / 'renamed'; canonical = root / 'platform/steam'
        canonical.mkdir(parents=True); (canonical / 'PBZ.exe').write_bytes(b'MZ')
        self.assertIsNone(gameprofiles.target_for(
            root, 'Phantom Blade Zero fan game', 'My PBZ collection'))
        unrelated = gameprofiles.recommendation(
            {'matched': True, 'game': 'My PBZ collection'}, 'My renamed title')
        plan = gameprofiles.constrain({}, unrelated)
        self.assertNotIn('required_target', plan)
        self.assertFalse(unrelated.get('required_target'))

    def test_fuzzy_pbz_wiki_match_does_not_grant_pbz_profile_or_target_identity(self):
        root = self.root / 'Phantom Blade Zero fan game'
        canonical = root / 'platform/steam'
        canonical.mkdir(parents=True)
        (canonical / 'PBZ.exe').write_bytes(b'MZ')
        entries = [{'name': 'Phantom Blade Zero', 'key': 'phantombladezero',
                    'page': None, 'compatibility': '✅', 'inputs': 'DLSSG via SL',
                    'optipatcher': False, 'notes': ''}]
        base_plan = {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                     'settings': []}
        with patch.object(self.service.wiki, 'load_entries',
                          return_value=(entries, {'source': 'test-cache'})), \
             patch('optiscaler.service.autoplan.build', return_value=base_plan):
            planned = asyncio.run(self.service.get_auto_plan(
                'Phantom Blade Zero fan game', game_path=str(root)))

        detail = asyncio.run(self.service.get_game(str(root), 'Phantom Blade Zero fan game'))
        self.assertEqual(planned['recommendation']['game'], 'Phantom Blade Zero')
        self.assertGreater(planned['recommendation']['match_score'], 0.78)
        self.assertNotIn('required_target', planned['recommendation'])
        self.assertNotIn('required_target', planned['plan'])
        self.assertFalse(detail['pbz_identity'])
        self.assertIsNone(detail['pbz_target'])

    def test_missing_pinned_pbz_row_uses_offline_profile_without_unrelated_match(self):
        root = self.root / 'Renamed shortcut'
        self.service.settings.set_wiki_entry(str(root), 'Phantom Blade Zero')
        empty = {'matched': False, 'searched': [], 'entry_count': 2,
                 'list_available': True, 'near_misses': [], 'game': None,
                 'page': None, 'filename': 'dxgi.dll', 'filename_source': 'default',
                 'alternatives': [], 'compatibility': None, 'inputs': None,
                 'notes': None, 'optipatcher': False, 'wiki_url': None,
                 'detail': {}, 'detail_pending': False, 'list_meta': {'source': 'cache'},
                 'match_score': None}
        unrelated = {**empty, 'matched': True, 'game': 'Phantom Blade Zero fan game',
                     'detail': {'Notes': 'unrelated settings'}, 'filename': 'winmm.dll'}

        def recommend(query, extra_names=None, force=False):
            return empty if not query else unrelated

        base_plan = {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                     'settings': []}
        with patch.object(self.service.wiki, 'entry_by_name', return_value=None), \
             patch.object(self.service.wiki, 'recommend', side_effect=recommend) as recommend_mock, \
             patch('optiscaler.service.autoplan.build', return_value=base_plan):
            planned = asyncio.run(self.service.get_auto_plan(
                'My renamed Steam shortcut', game_path=str(root)))

        recommend_mock.assert_called_once_with('', [], False)
        recommendation = planned['recommendation']
        self.assertTrue(recommendation['matched'])
        self.assertEqual(recommendation['game'], 'Phantom Blade Zero')
        self.assertEqual(recommendation['filename_source'], 'reviewed wiki profile')
        self.assertEqual(recommendation['detail']['Notes'],
                         'Keep Super Resolution Output Scale at 100%; only change the upscaler preset.')
        self.assertNotEqual(recommendation['detail'].get('Notes'), 'unrelated settings')
        self.assertEqual(recommendation['searched'], ['My renamed Steam shortcut'])
        self.assertEqual(planned['plan']['required_target'], 'PBZ/platform/steam')

    def test_generic_automatic_setup_and_settings_keep_their_existing_path_rules(self):
        root = self.root / 'Generic Game'
        target = root / 'platform/steam'
        target.mkdir(parents=True)
        (target / 'PBZ.exe').write_bytes(b'MZ')
        self.service.settings.set_wiki_entry(str(root), 'Unrelated Compatibility Entry')
        detail = asyncio.run(self.service.get_game(str(root), 'Generic Game'))
        self.assertIsNone(detail['pbz_target'])

        planned = {'recommendation': {'matched': True, 'game': 'Generic Game'},
                   'plan': {'available': True, 'filename': 'dxgi.dll', 'optipatcher': False,
                            'settings': []}}
        self.service.get_auto_plan = AsyncMock(return_value=planned)
        self.service.install = AsyncMock(return_value={'ok': True, 'applied': []})
        other_pbz_path = str(self.root / 'Other PBZ game')
        self.service.settings.set_pref('deck-experiment:' + other_pbz_path, True)
        frame_changes = [{'section': 'FrameGen', 'key': 'FGInput', 'value': 'fsrfg'}]
        written = []

        def record_write(directory, changes):
            written.extend(changes)
            return {'ok': True, 'applied': list(changes), 'rejected': []}

        self.service._write_config_sync = record_write
        with patch('optiscaler.service.autoplan.setting_changes', return_value=[]), \
             patch('optiscaler.service.autoplan.framegen_changes', return_value=frame_changes):
            installed = asyncio.run(self.service.auto_install(
                str(target), str(root), 'Generic Game'))
            configured = asyncio.run(self.service.apply_auto_settings(
                str(target), str(root), 'Generic Game'))
        self.assertTrue(installed['ok'], installed)
        self.assertTrue(configured['ok'], configured)
        self.assertEqual(written, frame_changes)


class PayloadCompletionTests(unittest.TestCase):
    def test_committed_cache_rejects_extra_nested_files_and_null_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = payload.Payload(Path(tmp) / 'payload.7z', Path(tmp) / 'cache')
            item.root.mkdir(parents=True)
            real = json.loads((ROOT / 'defaults/runtime-manifest.json').read_text())['files']
            pinned = {}
            for name in real:
                path = item.root / name; path.parent.mkdir(parents=True, exist_ok=True)
                data = ('fixture ' + name).encode(); path.write_bytes(data)
                pinned[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            with patch.object(payload, '_PINNED_FILES', pinned):
                inventory = payload._inventory_state(item.root)
                self.assertIsNotNone(inventory)
                marker = item.root / payload.COMMIT_MARKER
                record = {'version': constants.OPTISCALER_VERSION,
                          'archive_sha256': constants.PAYLOAD_SHA256, 'inventory': inventory}
                marker.write_text(json.dumps(record))
                self.assertTrue(item.ready)
                extra = item.root / 'D3D12_Optiscaler/unpinned.dll'; extra.write_bytes(b'extra')
                self.assertFalse(item.ready)
                extra.unlink()
                (item.root / 'D3D12_Optiscaler/D3D12Core.dll').unlink()
                for invalid in (None, {}, [], 'inventory'):
                    record['inventory'] = invalid; marker.write_text(json.dumps(record))
                    self.assertFalse(item.ready)

    def test_a_marker_without_the_required_inventory_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'payload.7z'; archive.write_bytes(b'archive')
            item = payload.Payload(archive, Path(tmp) / 'cache')
            item.root.mkdir(parents=True)
            (item.root / 'OptiScaler.dll').write_bytes(b'partial extraction')
            self.assertFalse(item.ready)


if __name__ == '__main__':
    unittest.main()

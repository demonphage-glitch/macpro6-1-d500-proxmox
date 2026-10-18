"""Synthetic fixtures only: no redistributed hardware firmware."""
import base64
import importlib.util
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('setup', Path(__file__).resolve().parents[1] / 'tools/d500-setup.py')
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def table(bus=6, checksum=True):
    rom = bytearray(65536)
    rom[:3] = b'\x55\xaa\x80'
    struct.pack_into('<H', rom, 0x18, 0x60)
    rom[0x60:0x64] = b'PCIR'
    struct.pack_into('<HH', rom, 0x64, 0x1002, 0x679e)
    struct.pack_into('<H', rom, 0x70, 128)
    rom[0x75] = 0x80
    struct.pack_into('<H', rom, 0x48, 0x80)
    rom[0x84:0x88] = b'ATOM'
    board = b'113-C3861LB-029'
    rom[0x100:0x100 + len(board)] = board
    rom[-1] = (-sum(rom)) % 256
    header = struct.pack('<IIIHHHHII', bus, 0, 0, 0x1002, 0x679e, 0x106b, 0x0125, 0, len(rom))
    data = bytearray(76) + header + rom
    data[:4] = b'VFCT'
    struct.pack_into('<I', data, 4, len(data))
    struct.pack_into('<II', data, 52, 76, 0)
    if checksum:
        data[9] = (-sum(data)) % 256
    return bytes(data)


def host():
    return dict(model='MacPro6,1', devices=[dict(address='0000:06:00.' + str(n),
                device=dev, subsystem='0x106b:0x0125', driver='vfio-pci', power='D3hot',
                group=['0000:06:00.0', '0000:06:00.1'])
                for n, dev in enumerate(('0x679e', '0xaaa0'))])


class FirmwareTests(unittest.TestCase):
    def test_metadata_mapping_and_hash(self):
        image, = setup.vfct_images(table())
        self.assertEqual(image['address'], '0000:06:00.0')
        self.assertEqual(image['subsystem'], '0x106b:0x0125')
        self.assertEqual(image['size'], 65536)
        self.assertEqual(image['sha256'], setup.digest(image['data']))

    def test_corrupt_acpi_checksum(self):
        data = bytearray(table())
        data[20] ^= 1
        with self.assertRaisesRegex(ValueError, 'checksum'):
            setup.vfct_images(data)

    def test_truncated_table(self):
        with self.assertRaises(ValueError):
            setup.vfct_images(table()[:-1])

    def test_reject_invalid_pci_address(self):
        with self.assertRaisesRegex(ValueError, 'PCI address'):
            setup.vfct_images(table(bus=256))

    def test_inner_rom_validation_not_just_acpi(self):
        data = bytearray(table())
        data[104] = 0
        data[9] = 0
        data[9] = (-sum(data)) % 256
        with self.assertRaisesRegex(ValueError, 'ROM signature/checksum'):
            setup.vfct_images(data)

    def test_truncated_image_record(self):
        data = bytearray(table())
        struct.pack_into('<I', data, 100, 1000000)
        data[9] = 0
        data[9] = (-sum(data)) % 256
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            setup.vfct_images(data)


class PlanTests(unittest.TestCase):
    def test_customize_and_refuse_wrong_firmware_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            vfct = Path(temp) / 'vfct'
            vfct.write_bytes(table())
            with patch.object(setup, 'preflight', return_value=(host(), {'machine': 'q35'})):
                plan = setup.make_plan(222, '0000:06:00.0', vfct)
                helper = base64.b64decode(plan['files']['/usr/local/sbin/d500-vm-start']).decode()
                self.assertIn('VMID=222', helper)
                self.assertIn('local checks=6', helper)
                self.assertTrue(plan['after']['hostpci0'].startswith('06:00,pcie=1'))
                vfct.write_bytes(table(bus=2))
                with self.assertRaisesRegex(ValueError, 'uniquely match'):
                    setup.make_plan(222, '0000:06:00.0', vfct)

    def test_subsystem_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            vfct = Path(temp) / 'vfct'
            vfct.write_bytes(table())
            h = host()
            h['devices'][0]['subsystem'] = '0x106b:0x0126'
            with patch.object(setup, 'preflight', return_value=(h, {})):
                with self.assertRaises(ValueError):
                    setup.make_plan(222, '0000:06:00.0', vfct)

    def test_preflight_refuses_shared_group_and_wrong_driver(self):
        for field, value in [('group', ['0000:06:00.0', '0000:07:00.0']), ('driver', 'radeon')]:
            h = host()
            h['devices'][0][field] = value
            with patch.object(setup, 'inventory', return_value=h):
                with self.assertRaisesRegex(ValueError, 'VFIO'):
                    setup.preflight(222, '0000:06:00.0')

    def test_running_vm_not_stopped(self):
        with patch.object(setup, 'inventory', return_value=host()), patch.object(setup, 'run', return_value='status: running') as run:
            with self.assertRaisesRegex(ValueError, 'must be stopped'):
                setup.preflight(222, '0000:06:00.0')
            self.assertEqual(run.call_args.args, ('qm', 'status', '222'))


class TransactionTests(unittest.TestCase):
    def test_install_failure_leaves_recovery_record(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            target = folder / 'new-file'
            plan = dict(host=setup.socket.gethostname(), vmid=222, before={}, after={'onboot': '0'},
                        files={str(target): base64.b64encode(b'new').decode()})
            state = folder / 'state'
            with patch.object(setup, 'STATE', state), patch.object(setup, 'root'), patch.object(setup, 'make_plan', return_value=plan), patch.object(setup, 'run', side_effect=OSError('synthetic failure')), patch('builtins.input', return_value='INSTALL 222'):
                with self.assertRaises(OSError):
                    setup.install(222, '', folder / 'vfct')
            record, = state.glob('*.json')
            txn = json.loads(record.read_text())
            self.assertEqual(txn['changed'], [str(target)])
            self.assertFalse(txn['vm_changed'])
            self.assertEqual(txn['status'], 'applying')

    def test_rollback_refuses_file_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            target = state / 'helper'
            target.write_bytes(b'user edited this')
            record = state / 'record.json'
            setup.save(record, dict(plan=dict(host=setup.socket.gethostname(), vmid=222,
                       files={str(target): base64.b64encode(b'installed').decode()}),
                       status='installed', changed=[str(target)], vm_changed=False))
            with patch.object(setup, 'STATE', state), patch.object(setup, 'root'), patch.object(setup, 'run', return_value='status: stopped'):
                with self.assertRaisesRegex(ValueError, 'File changed'):
                    setup.rollback(record)
            self.assertEqual(target.read_bytes(), b'user edited this')

    def test_install_and_rollback_restore_files_and_vm_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            target = folder / 'helper'
            target.write_bytes(b'old helper')
            target.chmod(0o644)
            fresh = folder / 'rom'
            plan = dict(host=setup.socket.gethostname(), vmid=222,
                        before={'onboot': '1'}, after={'hostpci0': '06:00,pcie=1', 'onboot': '0'},
                        files={str(target): base64.b64encode(b'new helper').decode(),
                               str(fresh): base64.b64encode(b'local synthetic firmware').decode()})
            state = folder / 'state'
            with patch.object(setup, 'STATE', state), patch.object(setup, 'root'), patch.object(setup, 'make_plan', return_value=plan), patch.object(setup, 'run', return_value='') as run, patch('builtins.input', return_value='INSTALL 222'):
                setup.install(222, '0000:06:00.0', folder / 'vfct')
            self.assertEqual(target.read_bytes(), b'new helper')
            self.assertTrue(fresh.exists())
            record, = state.glob('*.json')
            self.assertEqual(json.loads(record.read_text())['status'], 'installed')
            with patch.object(setup, 'STATE', state), patch.object(setup, 'root'), patch.object(setup, 'run', return_value='status: stopped') as run, patch.object(setup, 'config', return_value=plan['after']), patch('builtins.input', return_value='ROLLBACK 222'):
                setup.rollback(record)
            self.assertEqual(target.read_bytes(), b'old helper')
            self.assertFalse(fresh.exists())
            self.assertIn(unittest.mock.call('qm', 'set', '222', '--onboot', '1'), run.call_args_list)
            self.assertEqual(json.loads(record.read_text())['status'], 'rolled-back')

    def test_cancel_has_no_mutations(self):
        with patch.object(setup, 'root'), patch.object(setup, 'make_plan', return_value={'files': {}}), patch('builtins.input', return_value='no'), patch.object(setup, 'save') as save:
            with self.assertRaisesRegex(ValueError, 'Cancelled'):
                setup.install(222, '', Path('unused'))
            save.assert_not_called()

    def test_validate_dropout_does_not_stop_vm(self):
        with patch.object(setup, 'root'), patch.object(setup, 'run', return_value='status: running') as run, patch.object(setup.time, 'sleep'), patch.object(setup, 'qmp_ids', side_effect=[{'hostpci0.0', 'hostpci0.1'}, set()]):
            with self.assertRaisesRegex(ValueError, 'sample 2/6'):
                setup.validate(222)
            run.assert_called_once_with('qm', 'status', '222')

    def test_qmp_ignores_events_and_matches_ids(self):
        class Stream(io.BytesIO):
            def __init__(self):
                super().__init__()
                self.lines = iter([b'{"QMP":{}}\n', b'{"event":"RESET"}\n',
                                   b'{"id":"qmp_capabilities","return":{}}\n',
                                   b'{"event":"RESUME"}\n',
                                   b'{"id":"unrelated","return":{}}\n',
                                   b'{"id":"query-pci","return":[{"devices":[{"qdev_id":"hostpci0.0"},{"qdev_id":"hostpci0.1"}]}]}\n'])
            def readline(self):
                return next(self.lines)
        stream = Stream()
        with patch.object(setup.socket, 'AF_UNIX', 1, create=True), patch.object(setup.socket, 'socket') as socket:
            socket.return_value.__enter__.return_value.makefile.return_value = stream
            self.assertEqual(setup.qmp_ids(Path('fake-socket')), {'hostpci0.0', 'hostpci0.1'})


if __name__ == '__main__':
    unittest.main()

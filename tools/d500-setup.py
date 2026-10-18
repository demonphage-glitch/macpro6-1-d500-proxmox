#!/usr/bin/env python3
"""Local-only D500 assistant. Python 3.9+, standard library only."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[1]
PCI = Path('/sys/bus/pci/devices')
STATE = Path('/var/lib/d500-setup')
SERVICE = 'd500-vm-start.service'


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=120).strip()


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory():
    devices = []
    for p in sorted(PCI.glob('*')):
        if read(p / 'vendor') != '0x1002' or read(p / 'device') not in ('0x679e', '0xaaa0'):
            continue
        group = p / 'iommu_group'
        devices.append(dict(address=p.name, device=read(p / 'device'),
                            subsystem=read(p / 'subsystem_vendor') + ':' + read(p / 'subsystem_device'),
                            driver=(p / 'driver').resolve().name if (p / 'driver').exists() else '',
                            power=read(p / 'power_state'),
                            group=sorted(x.name for x in (group / 'devices').glob('*'))))
    return dict(model=read('/sys/class/dmi/id/product_name'), devices=devices,
                vfct_available=Path('/sys/firmware/acpi/tables/VFCT').exists())


def vfct_images(data):
    """Parse VFCT VBIOS entries using 28-byte image headers, never fixed ROM offsets."""
    if len(data) < 76 or data[:4] != b'VFCT':
        raise ValueError('Not a complete VFCT table')
    length = struct.unpack_from('<I', data, 4)[0]
    if length != len(data) or sum(data) % 256:
        raise ValueError('VFCT length/checksum mismatch')
    offset, lib = struct.unpack_from('<II', data, 52)
    end = lib if lib else length
    if not 76 <= offset < end <= length:
        raise ValueError('Invalid VFCT image region')
    images = []
    while offset < end:
        if offset + 28 > end:
            raise ValueError('Truncated VFCT image header')
        bus, dev, fn, vendor, device, sv, ss, revision, size = struct.unpack_from('<IIIHHHHII', data, offset)
        start = offset + 28
        if not size or start + size > end or bus > 255 or dev > 31 or fn > 7:
            raise ValueError('Invalid VFCT image dimensions/PCI address')
        image = data[start:start + size]
        if (vendor, device) == (0x1002, 0x679e):
            if len(image) < 0x1c or image[:2] != b'\x55\xaa' or sum(image) % 256:
                raise ValueError('D500 ROM signature/checksum failed')
            pcir = struct.unpack_from('<H', image, 0x18)[0]
            if pcir < 0x1c or pcir + 0x16 > len(image) or image[pcir:pcir + 4] != b'PCIR':
                raise ValueError('Missing PCI ROM data structure')
            if struct.unpack_from('<HH', image, pcir + 4) != (vendor, device):
                raise ValueError('VFCT/ROM device identity mismatch')
            # This first release deliberately supports the tested single-image legacy ROM form.
            if image[2] * 512 != size or struct.unpack_from('<H', image, pcir + 0x10)[0] * 512 != size:
                raise ValueError('Unsupported ROM size or multi-image layout')
            if image[pcir + 0x14] != 0 or not image[pcir + 0x15] & 0x80:
                raise ValueError('Only a complete single legacy PCI ROM image is supported')
            atom = struct.unpack_from('<H', image, 0x48)[0] if len(image) >= 0x4a else 0
            if atom < 0x1c or atom + 8 > size or image[atom + 4:atom + 8] not in (b'ATOM', b'MOTA'):
                raise ValueError('Missing ATOM BIOS header')
            board = re.search(rb'113-[A-Za-z0-9-]+', image)
            images.append(dict(address=f'0000:{bus:02x}:{dev:02x}.{fn}',
                               subsystem=f'0x{sv:04x}:0x{ss:04x}',
                               board=board.group().decode() if board else 'unknown',
                               sha256=digest(image), size=size, data=image))
        offset = start + size
    if not images:
        raise ValueError('No validated D500 images in VFCT')
    return images


def config(vmid):
    return dict(line.split(': ', 1) for line in run('qm', 'config', str(vmid)).splitlines() if ': ' in line)


def preflight(vmid, gpu):
    host = inventory()
    if host['model'] != 'MacPro6,1':
        raise ValueError('Only MacPro6,1 is supported in this release')
    if not re.fullmatch(r'0000:[0-9a-f]{2}:[0-1][0-9a-f]\.0', gpu):
        raise ValueError('Use the full GPU PCI address, e.g. 0000:06:00.0')
    chosen = {gpu, gpu[:-1] + '1'}
    devices = {x['address']: x for x in host['devices']}
    if any(x not in devices for x in chosen):
        raise ValueError('GPU/audio pair not found')
    if devices[gpu]['device'] != '0x679e' or devices[gpu[:-1] + '1']['device'] != '0xaaa0':
        raise ValueError('Unexpected GPU/audio identity')
    for address in chosen:
        d = devices[address]
        if d['driver'] != 'vfio-pci' or not d['group'] or set(d['group']) - chosen:
            raise ValueError('Both functions must already use VFIO and have groups containing only this pair')
    if 'status: stopped' != run('qm', 'status', str(vmid)):
        raise ValueError('VM must be stopped; this tool never stops an existing guest')
    cfg = config(vmid)
    if cfg.get('machine', '').split(',')[0] != 'q35':
        raise ValueError('Configure and verify a q35 VM first')
    if any(k.startswith('hostpci') for k in cfg):
        raise ValueError('VM already has passthrough configuration; review it manually before setup')
    if 'lock' in cfg or cfg.get('template') == '1':
        raise ValueError('Locked VMs and templates are unsupported')
    # Refuse any PCI assignment elsewhere: resource mappings are ambiguous for v1.
    for path in Path('/etc/pve/qemu-server').glob('*.conf'):
        if path.stem != str(vmid) and re.search(r'^hostpci\d+:', path.read_text(), re.M):
            raise ValueError('Another VM has PCI passthrough; v1 requires manual ownership review')
    if Path('/etc/pve/ha/resources.cfg').exists() and re.search(rf'^vm:\s*{vmid}\s*$', read('/etc/pve/ha/resources.cfg'), re.M):
        raise ValueError('Remove this VM from HA before assigning startup to this helper')
    for name in ('d500-wake.service', 'd500-jellyfin.service', SERVICE):
        for action in ('is-enabled', 'is-active'):
            result = subprocess.run(['systemctl', action, name], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                raise ValueError(f'{name} is enabled/active; disable it before setup')
    return host, cfg


def make_plan(vmid, gpu, vfct):
    host, cfg = preflight(vmid, gpu)
    matches = [x for x in vfct_images(vfct.read_bytes()) if x['address'] == gpu and
               x['subsystem'] == next(d['subsystem'] for d in host['devices'] if d['address'] == gpu)]
    if len(matches) != 1:
        raise ValueError('Firmware must uniquely match PCI address AND subsystem identity')
    image = matches[0]
    rom = 'd500-' + image['sha256'][:16] + '.rom'
    helper = (REPO / 'scripts/d500-vm-start').read_text().replace('VMID=103', f'VMID={vmid}').replace('0000:06:00.0', gpu).replace('0000:06:00.1', gpu[:-1] + '1')
    unit = (REPO / 'systemd/d500-vm-start.service').read_text()
    files = {'/usr/local/sbin/d500-vm-start': helper.encode(),
             '/etc/systemd/system/' + SERVICE: unit.encode(),
             '/usr/share/kvm/' + rom: image['data']}
    return dict(host=socket.gethostname(), inventory=host, vmid=vmid, gpu=gpu, before=cfg,
                after={'hostpci0': f'{gpu[5:-2]},pcie=1,romfile={rom}', 'onboot': '0'},
                firmware={k: v for k, v in image.items() if k != 'data'},
                files={p: base64.b64encode(b).decode() for p, b in files.items()})


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(obj, indent=2) + '\n')
    temp.replace(path)


def root():
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('This action requires root on the Proxmox host')


def install(vmid, gpu, vfct):
    root()
    plan = make_plan(vmid, gpu, vfct)
    print(json.dumps({k: v for k, v in plan.items() if k != 'files'}, indent=2))
    print('Will install local firmware/helper/unit, set hostpci0 and onboot=0, and enable startup.')
    print('No bootloader/VFIO changes, reboot, or VM start. Disable other custom startup paths first.')
    if input(f'Type INSTALL {vmid} to apply: ') != f'INSTALL {vmid}':
        raise ValueError('Cancelled without changes')
    # Recheck state after the review prompt. No saved editable plan is ever executed.
    if make_plan(vmid, gpu, vfct) != plan:
        raise ValueError('Host, VM, firmware, or templates changed during review')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    record = STATE / (str(time.time_ns()) + '.json')
    backup = {}
    for name in plan['files']:
        p = Path(name)
        if p.is_symlink() or (p.exists() and not p.is_file()):
            raise ValueError(f'Unexpected installation target: {p}')
        backup[name] = {'data': base64.b64encode(p.read_bytes()).decode(), 'mode': p.stat().st_mode & 0o777} if p.exists() else None
    txn = dict(plan=plan, backup=backup, changed=[], vm_changed=False, enabled=False, status='applying')
    save(record, txn)
    print(f'Rollback record: {record}')
    try:
        for name, encoded in plan['files'].items():
            txn['changed'].append(name)
            save(record, txn)
            p = Path(name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(encoded))
            p.chmod(0o755 if name.startswith('/usr/local/sbin/') else 0o644)
        run('bash', '-n', '/usr/local/sbin/d500-vm-start')
        txn['vm_changed'] = True
        save(record, txn)
        run('qm', 'set', str(vmid), '--hostpci0', plan['after']['hostpci0'], '--onboot', '0')
        run('systemctl', 'daemon-reload')
        run('systemd-analyze', 'verify', '/etc/systemd/system/' + SERVICE)
        txn['enabled'] = True
        save(record, txn)
        run('systemctl', 'enable', SERVICE)
        txn['status'] = 'installed'
        save(record, txn)
        print('Installed. VM remains stopped. Validate manually before a planned cold boot.')
    except Exception:
        print(f'Install interrupted/failed. Use rollback {record}; inspect errors before booting.', file=sys.stderr)
        raise


def rollback(record):
    root()
    record = record.resolve()
    if record.parent != STATE.resolve() or record.is_symlink():
        raise ValueError('Use a local installer record under /var/lib/d500-setup')
    txn = json.loads(record.read_text())
    p = txn['plan']
    if p['host'] != socket.gethostname() or txn['status'] == 'rolled-back':
        raise ValueError('Wrong host or already rolled back')
    if run('qm', 'status', str(p['vmid'])) != 'status: stopped':
        raise ValueError('Stop the VM yourself before rollback')
    for name in txn['changed']:
        if not Path(name).is_file() or digest(Path(name).read_bytes()) != digest(base64.b64decode(p['files'][name])):
            raise ValueError(f'File changed since installation; manual review required: {name}')
    if txn['vm_changed']:
        current = config(p['vmid'])
        for key, value in p['after'].items():
            if current.get(key) not in (value, p['before'].get(key)):
                raise ValueError('VM settings changed; refusing to overwrite them')
    if input(f"Type ROLLBACK {p['vmid']} to restore: ") != f"ROLLBACK {p['vmid']}":
        raise ValueError('Cancelled')
    if txn['enabled']:
        run('systemctl', 'disable', SERVICE)
    if txn['vm_changed']:
        for key in p['after']:
            if key in p['before']:
                run('qm', 'set', str(p['vmid']), '--' + key, p['before'][key])
            elif key in config(p['vmid']):
                run('qm', 'set', str(p['vmid']), '--delete', key)
    for name in txn['changed']:
        old = txn['backup'][name]
        if old is None:
            Path(name).unlink(missing_ok=True)
        else:
            Path(name).write_bytes(base64.b64decode(old['data']))
            Path(name).chmod(old['mode'])
    run('systemctl', 'daemon-reload')
    txn['status'] = 'rolled-back'
    save(record, txn)
    print('Restored the settings/files changed by this installer. VM remains stopped.')


def qmp_ids(path):
    """Ignore asynchronous events and associate replies with explicit command IDs."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        with sock.makefile('rwb') as stream:
            if 'QMP' not in json.loads(stream.readline()):
                raise ValueError('Invalid QMP greeting')
            for command in ('qmp_capabilities', 'query-pci'):
                stream.write((json.dumps({'execute': command, 'id': command}) + '\n').encode())
                stream.flush()
                deadline = time.monotonic() + 5
                while True:
                    if time.monotonic() > deadline:
                        raise ValueError('QMP response deadline exceeded')
                    response = json.loads(stream.readline())
                    if response.get('id') == command:
                        break
                if 'error' in response:
                    raise ValueError('QMP command failed: ' + str(response['error']))
            def walk(obj):
                found = set()
                if isinstance(obj, dict):
                    if isinstance(obj.get('qdev_id'), str):
                        found.add(obj['qdev_id'])
                    for value in obj.values():
                        found |= walk(value)
                elif isinstance(obj, list):
                    for value in obj:
                        found |= walk(value)
                return found
            return walk(response.get('return'))


def validate(vmid, guest=False):
    root()
    if run('qm', 'status', str(vmid)) != 'status: running':
        raise ValueError('VM must already be running; validation never starts/stops it')
    for index in range(6):
        time.sleep(5)
        if not {'hostpci0.0', 'hostpci0.1'} <= qmp_ids(Path(f'/run/qemu-server/{vmid}.qmp')):
            raise ValueError(f'GPU/audio attachment missing at sample {index + 1}/6')
        print(f'QMP sample {index + 1}/6 PASS', flush=True)
    print('Attachment sampling passed. Guest driver health requires a separate check.')
    if guest:
        print(run('qm', 'guest', 'exec', str(vmid), '--', 'bash', '-lc',
                  "lspci -nnk; ls -l /dev/dri; dmesg | grep -Ei 'ATOM BIOS|UVD initialized|VCE initialized'"))
        print('Review guest output for radeon, render node and UVD/VCE. This is not a transcoding test.')


def wizard():
    print('D500 setup assistant — local firmware only; nothing is uploaded.')
    print(json.dumps(inventory(), indent=2))
    print('1. Extract firmware  2. Preview setup  3. Install setup  4. Validate running VM  0. Exit')
    choice = input('Choose: ').strip()
    if choice == '0':
        return 0
    if choice == '1':
        return main(['extract', input('VFCT file: '), input('New output directory: ')])
    if choice in ('2', '3'):
        print('Available VMs:\n' + run('qm', 'list'))
        vmid = input('VM ID (no default): ').strip()
        gpu = input('Full GPU address (no default): ').strip()
        vfct = input('Your local VFCT file: ').strip()
        return main(['plan' if choice == '2' else 'install', '--vmid', vmid, '--gpu', gpu, '--vfct', vfct])
    if choice == '4':
        return main(['validate', '--vmid', input('Running VM ID: ').strip()])
    raise ValueError('Unknown menu choice')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('inspect', help='Read-only host report')
    extract = commands.add_parser('extract', help='Validate and extract local firmware (never uploads)')
    extract.add_argument('vfct', type=Path)
    extract.add_argument('directory', type=Path)
    for cmd in ('plan', 'install'):
        sub = commands.add_parser(cmd)
        sub.add_argument('--vmid', type=int, required=True)
        sub.add_argument('--gpu', required=True)
        sub.add_argument('--vfct', type=Path, required=True)
    undo = commands.add_parser('rollback')
    undo.add_argument('record', type=Path)
    check = commands.add_parser('validate')
    check.add_argument('--vmid', type=int, required=True)
    check.add_argument('--guest', action='store_true', help='Request read-only guest diagnostics through QEMU Guest Agent')
    argv = sys.argv[1:] if argv is None else argv
    try:
        if not argv:
            return wizard()
        args = parser.parse_args(argv)
        if args.command == 'inspect':
            print(json.dumps(inventory(), indent=2))
        elif args.command == 'extract':
            images = vfct_images(args.vfct.read_bytes())
            args.directory.mkdir(parents=True, exist_ok=True)
            for image in images:
                path = args.directory / (image['address'].replace(':', '-') + '-' + image['sha256'][:16] + '.rom')
                with path.open('xb') as f:
                    f.write(image.pop('data'))
                print(json.dumps(dict(path=str(path), **image)))
        elif args.command in ('plan', 'install'):
            if args.vmid < 100:
                raise ValueError('VM ID must be at least 100')
            if args.command == 'install':
                install(args.vmid, args.gpu, args.vfct)
            else:
                plan = make_plan(args.vmid, args.gpu, args.vfct)
                plan['files'] = {p: {'size': len(base64.b64decode(b)), 'sha256': digest(base64.b64decode(b))} for p, b in plan['files'].items()}
                print(json.dumps(plan, indent=2))
        elif args.command == 'validate':
            if args.vmid < 100:
                raise ValueError('VM ID must be at least 100')
            validate(args.vmid, args.guest)
        else:
            rollback(args.record)
    except (ValueError, OSError, subprocess.SubprocessError, EOFError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

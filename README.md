# Apple FirePro D500 passthrough on Proxmox

A tested startup workaround for one Apple Mac Pro 6,1 (Late 2013) with dual FirePro D500 GPUs. The important discovery: **a running VM does not prove that its GPU remained attached**. The V3 helper verifies both QEMU PCI functions every five seconds for a nominal 30-second window and automatically performs one second start after a failed first assignment.

Tested on October 13, 2026 with Proxmox VE 9.2.21, kernel 7.0.14-20-pve, QEMU package pve-qemu-kvm_11.0.3-4, and Ubuntu Server 24.04. This records that environment; it does not guarantee behavior on other firmware, hardware, or software versions.

## Downloads and repository contents

- [Full PDF guide](docs/Apple_FirePro_D500_Proxmox_Passthrough_Guide.pdf)
- [Editable Word guide](docs/Apple_FirePro_D500_Proxmox_Passthrough_Guide.docx)
- [V3 recovery helper](scripts/d500-vm-start)
- [systemd service](systemd/d500-vm-start.service)
- [Commented VFIO configuration](examples/vfio.conf.example)

The PDF and DOCX are the original completed guide. They call the helper `d500-jellyfin-start` and the service `d500-jellyfin.service`. This repository calls them `d500-vm-start` and `d500-vm-start.service`; the service executable path is adjusted accordingly. The helper retains its original `d500-jellyfin` journal tag and tested logic.

**No ROM files or VFCT binaries are distributed.** Obtain firmware from your own machine. Never flash these extracted ROMs: this procedure supplies a VBIOS to QEMU only.

## Values you must customize

| Tested value | Meaning and required review |
| --- | --- |
| `103` | VM ID; change `VMID` in the helper and every example command |
| `0000:06:00.0` / `0000:06:00.1` | GPU and audio; change `GPU` and `AUDIO` to your host addresses |
| `06:00` | Proxmox all-functions passthrough shorthand for the tested slot |
| `hostpci0.0` / `hostpci0.1` | QEMU IDs assumed by the embedded Python `wanted` set; verify with QMP or `info pci` and adjust if needed |
| `D500-LB-029.rom` | Tested board's firmware filename; use your own validated firmware |
| `1002:679e` / `1002:aaa0` | Observed GPU/audio PCI IDs; identify devices and groups before binding |
| `8086:1d20` | Another device reserved on the test host; **not a universal D500 requirement** |

Both D500s shared `1002:679e`. Binding by that ID can reserve both GPUs. If the host needs one GPU, use a selective binding strategy. Back up the host and VM, retain remote access, and verify IOMMU isolation before changes. The retry uses `qm stop`, a hard stop; consider guest writes that might occur during startup. Give this service sole ownership of automatic startup for the selected VM.

## What failed

Three cold-boot variants were observed:

1. **Physical disappearance:** GPU functions vanished from host PCI/sysfs, then re-enumerated under `vfio-pci`; the first QEMU instance remained running with an empty downstream bus.
2. **Empty QEMU bus:** host functions stayed visible, but QEMU's PCIe bridge contained no D500.
3. **Delayed dropout:** QMP initially showed both functions, passing the old five-second check, but they disappeared before Ubuntu enumerated them.

The helper therefore queries QEMU's PCI topology rather than relying on `qm start`, a host disappearance heuristic, or a single early check. QMP attachment is still distinct from guest driver health.

## 1. Inventory and early VFIO binding

Run on the Proxmox host; the following addresses are examples from the tested machine:

```bash
lspci -nnk | grep -A4 -Ei 'VGA|Display|Audio'
lspci -nnvv -s 06:00.0
lspci -nnvv -s 06:00.1
readlink -f /sys/bus/pci/devices/0000:06:00.0/iommu_group
readlink -f /sys/bus/pci/devices/0000:06:00.1/iommu_group
dmesg | grep -Ei 'DMAR|IOMMU'
```

Enable Intel IOMMU through the bootloader configuration appropriate to your installation. Start with the minimum required configuration, such as `intel_iommu=on`; the tested host also used `iommu=pt`. The original guide records extra host boot arguments, including ACS override. Do not copy ACS override blindly: changed apparent grouping does not create hardware isolation.

Review [vfio.conf.example](examples/vfio.conf.example), then adapt it for `/etc/modprobe.d/vfio.conf`. Use commas with **no spaces** in `ids=`; spaces caused later IDs to be ignored during this investigation. Load these modules early through `/etc/initramfs-tools/modules` on the tested setup:

```text
vfio
vfio_iommu_type1
vfio_pci
```

```bash
update-initramfs -u -k "$(uname -r)"
# After a planned reboot, verify both selected functions:
lspci -nnk -s 06:00.0
lspci -nnk -s 06:00.1
```

Both must show `vfio-pci` before the helper runs.

## 2. D3hot and the reset limitation

On cold boot the tested GPU could be bound to VFIO yet remain in **D3hot**. Correct ownership alone was insufficient. Setting runtime power control to `on` for both functions and waiting two seconds brought them to D0:

```bash
echo on > /sys/bus/pci/devices/0000:06:00.0/power/control
echo on > /sys/bus/pci/devices/0000:06:00.1/power/control
sleep 2
cat /sys/bus/pci/devices/0000:06:00.0/power_state
cat /sys/bus/pci/devices/0000:06:00.1/power_state
```

Use only for the dedicated devices while the VM is stopped. V3 performs this preparation and requires D0 before each start.

The GPU advertised `bus` reset and the audio function `pm`, but writing the GPU reset node failed with `Inappropriate ioctl for device`. The upstream port `00:03.0` on this host lacked a usable sysfs reset node. Proxmox logged the failure but continued assignment; this warning alone did not determine success. The workaround **does not repair reset support** and does not force bridge resets or raw PCI configuration writes. Inspect actual assignment and guest health.

## 3. Discover and validate your own Apple VBIOS

The normal PCI ROM sysfs path returned an I/O error and invalid `0x0000` signature even after native Radeon initialization. Apple's ACPI **VFCT** table contained the usable VBIOS images. If it is unavailable under Proxmox, a native Linux live environment can expose it; Ubuntu 24.04.5 Live was used during the investigation.

```bash
ls /sys/firmware/acpi/tables/
# If VFCT exists, copy it locally on your own Mac Pro:
cp /sys/firmware/acpi/tables/VFCT /root/VFCT.bin
sha256sum /root/VFCT.bin
```

Parse your own VFCT and correlate its image metadata with host PCI/subsystem identity. Validate the option ROM's `55 aa` signature, PCIR vendor/device identity (`1002:679e` on this host), ATOM BIOS board string, declared size, and byte checksum. Both tested images were 65,536 bytes with checksum `sum(image) & 0xff == 0`. A signature or matching device ID alone cannot establish which D500 owns the image.

The tested table was 131,232 bytes. Images began at `0x68` and `0x10084`; preceding metadata and board identity established this mapping:

| Board string | Tested physical GPU | Apple subsystem |
| --- | --- | --- |
| `113-C3861LA-029` | `02:00.0` | `106b:0126` |
| `113-C3861LB-029` | `06:00.0` | `106b:0125` |

These offsets and mappings are specific to the tested table. Do not split another table at those offsets without parsing and validation. This repository does not include a general VFCT extractor; the original guide records the discovery and validation evidence.

Reference SHA-256 fingerprints from **the tested machine only**:

```text
VFCT.bin (131232 bytes)
267ae5cd39050b2ffdd0d5911c3e860aa8fb5dce745c7dfc2e9aecb367fe65a8
D500-LA-029.rom (65536 bytes)
eb2aec73a30e93aa3867a34e4eb2eeca01c421780fff3dd5ca727ffa53227a99
D500-LB-029.rom (65536 bytes)
c07040282180be27bc795c8a8e4f579b9dd3bee20ef3cd0dc025d7fec9675d31
```

These are reproducibility data, not universal required hashes or a firmware download manifest.

## 4. Configure the VM

The tested VM used q35, CPU `host`, six cores, 8424 MB RAM, and retained emulated VGA. With the VM stopped, install your validated image in `/usr/share/kvm/` and configure both functions. The tested configuration was:

```text
hostpci0: 06:00,pcie=1,romfile=D500-LB-029.rom
```

```bash
qm showcmd 103 --pretty
```

Verify the actual ROM path and both functions in the generated command. The helper's QMP query expects `hostpci0.0` and `hostpci0.1`; another assignment slot or topology requires changing that set. The ROM is supplied to QEMU, never flashed to the GPU.

## 5. Install V3 and the service

First customize the helper's VM ID, PCI addresses, and QEMU IDs. The helper preserves the tested V3 algorithm: verify VFIO, wake to D0, start, wait for QMP, perform six checks separated by five-second sleeps, and on failure stop, wait for stopped state, settle for two seconds, verify VFIO and wake again, then retry once. It checks rather than waits indefinitely for VFIO ownership. A second failure attempts to stop the VM and exits with an error; inspect the actual VM state if stopping fails.

```bash
install -m 755 scripts/d500-vm-start /usr/local/sbin/d500-vm-start
install -m 644 systemd/d500-vm-start.service /etc/systemd/system/d500-vm-start.service
bash -n /usr/local/sbin/d500-vm-start
# Substitute your VM ID:
qm set 103 --onboot 0
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/d500-vm-start.service
systemctl enable d500-vm-start.service
```

Disable any previous wake/start service for this VM (including the original `d500-wake.service` or `d500-jellyfin.service` if installed). Avoid competing startup paths. The unit uses `After=` and `Requires=pve-guests.service`, which resolved premature startup on the tested host. Adding `graphical.target` previously caused an ordering problem and prevented startup.

An already running VM is protected: the helper checks attachment and exits without stopping it. That branch uses one QMP check, not the startup stabilization loop. The oneshot service's `active (exited)` state records completed startup; it is **not continuous monitoring**.

## 6. Validate a genuine cold boot and the guest

After preflight, perform a planned complete power-off/power-on. Do not manually wake the GPU or start the VM during the test. Allow both attempts to finish, then inspect:

```bash
qm status 103
systemctl status d500-vm-start.service --no-pager
journalctl -b -u d500-vm-start.service --no-pager
```

The October 13 test reproduced the delayed dropout: attempt 1 passed check 1 at 16:49:49 EDT and failed check 2 at 16:49:54. Stopping logged `no PCI device found for '06:00'`. Both functions reappeared under VFIO, reached D0, and attempt 2 passed all six checks through 16:50:41. Recovery required no manual intervention after power-on.

Wait for Ubuntu to boot, then use its console/SSH or QEMU Guest Agent to validate:

```bash
qm guest exec 103 -- bash -lc '
lspci -nnk | grep -A3 -E "VGA|Audio"
ls -l /dev/dri
dmesg | grep -Ei "ATOM BIOS|UVD initialized|VCE initialized" | tail -10
'
```

The successful guest showed `1002:679e` with `radeon`, `1002:aaa0` with `snd_hda_intel`, `/dev/dri/renderD128`, `ATOM BIOS: Tahiti`, and successful UVD/VCE initialization. An unavailable guest agent or black console after GRUB alone does not establish a GPU failure. Validate inside the guest when ready. These logs establish driver/video-engine initialization, not every Jellyfin codec or successful application transcoding; test actual workloads separately.

## Troubleshooting and limits

| Symptom | Check/action |
| --- | --- |
| VFIO bound but D3hot before start | Wake both functions; require D0 |
| Reset ioctl warning | Verify sustained QEMU attachment; avoid unsupported forced resets |
| Running VM but no GPU | Inspect `qm monitor YOUR_VMID`, then `info pci`; compare guest enumeration |
| Single QMP check passes, GPU later absent | Use the six-check V3 startup window |
| VFIO unknown parameter warnings | Remove spaces after commas and rebuild initramfs |
| Host devices absent or not VFIO after retry settling | Investigate enumeration and ownership; helper fails rather than looping |
| Early cluster readiness error | Review service ordering and actual host readiness |
| Two failed starts | Inspect journal, PCI state, configuration, and guest; no endless retries |

Six samples across a nominal 30 seconds caught the observed dropout; query overhead can extend the elapsed time. This is sampled verification, not proof of uninterrupted attachment or immunity to later failure. QMP verifies device IDs and topology, not complete guest functionality. The original QMP exchange expects command replies in sequence; unexpected asynchronous messages may cause a conservative verification failure. Re-test after kernel, QEMU, Proxmox, or firmware changes.

## License

Original scripts and documentation are released under the [MIT License](LICENSE). Apple/AMD firmware and other third-party material are not covered by that license. No firmware binaries are included.

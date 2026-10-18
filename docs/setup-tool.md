# Guided D500 setup assistant (experimental v0.1)

Run from a complete checkout of this repository on the Proxmox host. Python 3.9+ and the standard library are sufficient; the host also needs its normal `qm`, `systemctl`, `systemd-analyze`, and Bash tools. No pip packages, downloads, telemetry, or firmware uploads are used.

```bash
python3 tools/d500-setup.py
```

With no arguments the menu shows host inventory and offers firmware extraction, a configuration preview, installation, or validation. No VM or GPU is selected by default. Installation and rollback require root and an exact typed confirmation after the proposed settings are shown.

## Prepare the host

Follow the root guide to establish IOMMU isolation and early VFIO ownership first. This release intentionally does not edit the bootloader, enable ACS override, rebind devices, rebuild initramfs, reboot, stop a running VM, or flash firmware. The bundled VFIO example is a starting point for manual review, not something the tool installs.

Use a stopped q35 VM with no existing `hostpci` assignment. Both D500 functions must already use `vfio-pci`; each IOMMU group must contain only members of the selected GPU/audio pair. Disable previous D500 startup services and any other custom path that starts this VM. Remove the VM from HA. For this conservative first release, any PCI passthrough assignment in another VM blocks installation pending manual ownership review.

The tool supports the exact DMI model `MacPro6,1` and AMD GPU/audio IDs `1002:679e`/`1002:aaa0`. It rejects ambiguous configurations rather than guessing. A rejection is not evidence that passthrough is impossible.

## Obtain and inspect firmware locally

Copy `/sys/firmware/acpi/tables/VFCT` from your own Mac Pro to a local file. If it is unavailable under Proxmox, boot native Linux to obtain it, then return to Proxmox. The assistant cannot automate that physical boot transition.

```bash
python3 tools/d500-setup.py inspect
python3 tools/d500-setup.py extract /root/VFCT.bin /root/d500-local-firmware
```

The extractor validates the ACPI table checksum/length and parses VFCT image records, including 32-bit bus/device/function fields and 28-byte image headers. It checks the ROM signature, PCIR identity and lengths, byte checksum, and ATOM header. It reports the board string and SHA-256 for each accepted D500 image. Existing output files are never overwritten. An extraction failure can leave earlier successfully extracted files in the output directory.

The VFCT field layout follows the [Linux AMD ATOM BIOS definitions](https://github.com/torvalds/linux/blob/master/drivers/gpu/drm/amd/include/atombios.h) (`UEFI_ACPI_VFCT` and `VFCT_IMAGE_HEADER`). The parser is an independent Python implementation; it does not embed firmware or kernel code.

This release supports single-image legacy ROM layouts with consistent size fields. It deliberately rejects other layouts for manual review. These checks detect structural problems; they do not authenticate firmware or prove compatibility. Installation requires an exact match between VFCT PCI address/subsystem metadata and the current host. If native Linux enumerates your hardware differently, review the mapping manually; do not edit metadata to bypass the check.

Extracted ROMs are for QEMU only. Do not flash them or publish the output directory. Git ignore rules exclude firmware, but do not prevent deliberate forced uploads. Installer records also contain local firmware and must stay private to the host.

## Preview and install

The following VM ID and PCI address are examples; select your own:

```bash
python3 tools/d500-setup.py plan --vmid 103 --gpu 0000:06:00.0 --vfct /root/VFCT.bin
sudo python3 tools/d500-setup.py install --vmid 103 --gpu 0000:06:00.0 --vfct /root/VFCT.bin
```

`plan` is read-only and lists file hashes/sizes without embedding firmware in its output. `install` builds a fresh plan, displays the selected machine/VM/firmware and settings, and requires `INSTALL YOUR_VMID`. It rechecks the host after confirmation. The assistant does not load or execute a saved editable plan.

Installation writes:

- The customized, previously tested V3 helper to `/usr/local/sbin/d500-vm-start`.
- The matching service to `/etc/systemd/system/d500-vm-start.service`.
- The selected local firmware to `/usr/share/kvm/d500-HASH_PREFIX.rom`.
- A private backup/transaction record under `/var/lib/d500-setup/`.

It sets `hostpci0` to the selected GPU/audio slot with `pcie=1` and the local ROM filename, disables Proxmox `onboot`, reloads systemd, checks Bash syntax and the service definition, and enables the service for future boots. It leaves the VM stopped. Only one installation is supported per host because the existing service/helper use a single VM.

The original V3 helper still uses its tested QMP exchange and sampled startup checks. This tool does not silently replace its recovery logic. Hard-stop retry behavior and all limitations in the root guide still apply. Disabling `onboot` is not sufficient to prevent external schedulers or custom scripts from starting the VM: review those yourself. Host changes are not atomic; do not run competing configuration tools or start the VM during installation/rollback.

If installation fails or is interrupted, use the printed transaction record to review/restore changes before booting. The tool records intent before each mutation. A process failure during a file write, or changes made afterward, may require manual recovery; rollback will refuse file drift rather than discard it.

## Validate without changing guest state

After manually starting the VM at an appropriate time:

```bash
sudo python3 tools/d500-setup.py validate --vmid 103
sudo python3 tools/d500-setup.py validate --vmid 103 --guest
```

Validation samples both expected QEMU PCI IDs six times at five-second intervals. The diagnostic QMP client matches response IDs and ignores asynchronous events. It never starts, stops, wakes, or resets hardware. `--guest` additionally requests PCI, DRM and driver logs through an already available guest agent; read the returned guest exit status/output. It does not certify guest health automatically. Look for Radeon initialization, a render node, UVD and VCE, and test actual application workloads separately.

Then perform a planned power-off/power-on test and inspect the service journal as described in the root guide. The installer has been tested with synthetic firmware and mocked host operations, **not on a physical Mac Pro**. The original recovery helper has the previously documented cold-boot validation; the new setup workflow still needs hardware testing.

## Rollback

```bash
sudo python3 tools/d500-setup.py rollback /var/lib/d500-setup/RECORD.json
```

The VM must be stopped. Rollback refuses a different host, already restored records, changed installed files, or unrelated changes to the VM fields it owns. After `ROLLBACK YOUR_VMID`, it disables the new service, restores the previous `hostpci0`/`onboot` fields and overwritten files/modes, removes newly installed files, and reloads systemd. The transaction record remains for diagnosis. It does not restore unrelated settings or start anything. If rollback itself is interrupted partway through, inspect the record and restore remaining items manually rather than forcing a retry past drift checks.

## Developer checks

```bash
python3 -m unittest discover -s tests -v
```

Tests generate synthetic ROM/VFCT bytes and fake host operations. No Apple/AMD firmware fixtures are committed.

# FM350-GL Firmware Flashing & BROM / Boot-Pin Mode

> **Scope.** How to update the firmware on a **Fibocom FM350-GL** 5G M.2 module — the
> cellular modem inside the Photonicat 2. This is a hardware-maintenance procedure for
> a module you own. It covers entering MediaTek **BROM (BootROM) / download mode**,
> which is the *only* way to reflash this modem.
>
> ⚠️ **All firmware flashing risks bricking the module.** A wrong image, an interrupted
> flash, or a mismatched Download Agent can leave the modem permanently dead. Proceed
> only if you understand and accept that risk. There is no on-device / over-the-air
> update path (see [Why not on the device?](#why-not-on-the-device) below).

---

## 0. TL;DR

| | |
|---|---|
| **Modem** | Fibocom FM350-GL (MediaTek **T700**, USB VID `0e8d`) |
| **Current firmware (this unit)** | `81600.0000.00.29.20.30` (read via `AT+CGMR`) |
| **Latest known public** | `81600.0000.00.29.23.06` (Microsoft Update Catalog only) |
| **Normal USB id** | `0e8d:7127` |
| **BROM / download id (target)** | `0e8d:2000` (BootROM) / `0e8d:200x` (preloader VCOM) |
| **Flash host** | **x86_64** PC (Windows *or* Linux). **Not** the Photonicat itself. |
| **Flash tool** | SP Flash Tool v6.2124 (Win) / v6.22xx (Linux), *or* the Lenovo/Fibocom Linux tool |
| **Enter BROM** | Best via a USB power cut/short at connect, or a boot-config test point |

---

## Why not on the device?

The Photonicat is **aarch64 OpenWrt**. Flashing an MTK modem needs an x86-only, GUI/X11
flash tool (SP Flash Tool) *and* the modem forced into a low-level BROM window on a USB
bus the flash host controls directly. We verified on the device that **none of the
enablers exist**:

- Modem enumerates as `0e8d:7127` (normal), never `0e8d:2000`, and there is **no AT
  command to enter BROM/download mode** — probed and rejected:
  `AT+GTBROM`, `AT+GTDOWNLOAD`, `AT+GTFLASH`, `AT+GTPRELOADER` → all `+CME ERROR: unknown`.
  (Only `AT+GTFMODE`, `AT+GTUSBMODE=(40,41)`, `AT+CFUN=(0,1,4,15)` exist — none flash.)
- No MediaTek Download Agent (DA) for the T700 is present.
- The on-device `qfirehose` is **Qualcomm-only** and cannot touch a MediaTek part.
- An `mtkclient`/`mediatek_flash_tool` binary *can* be cross-compiled to aarch64 and will
  run on the Photonicat, but it still can't help: BROM entry is a **physical** act, and the
  open-source CLI tool is only validated on MT6580/EMMC phones — not a T700 modem.

**Conclusion:** reflashing is an **x86 + physical-access** operation. Do it on a PC with
the M.2 card in a USB/M.2 adapter (or, at minimum, on a host that owns the modem's USB bus).

---

## 1. What you need

**Hardware**
- The FM350-GL module.
- An **M.2 Key-B → USB** adapter (a "5G M.2 to USB 3.0" dev board), *or* an x86 laptop
  whose internal M.2 WWAN slot holds the card. The adapter is preferred because it gives
  you access to the boot/power pins.
- (For the test-point method) fine tweezers or a thin jumper wire; optionally a switched
  USB cable so you can cut VBUS on demand.

**Software (on an x86_64 PC)**
- **Windows:** SP Flash Tool **v6.2124**, MediaTek USB VCOM / Preloader drivers.
  Optionally `mtkclient` (needs libusb + a UsbDk/Zadig filter).
- **Linux:** SP Flash Tool for Linux (`flash_tool.sh`, x86_64, needs X11 + `libpng12`),
  **or** the official Lenovo/Fibocom Linux tool
  (`ds568880` Ubuntu / `ds569694` Fedora — x86_64 `.deb`/installer, ThinkPad-oriented),
  **or** `mtkclient` (`pip install`, uses `libusb`).

**Firmware package** — the `81600.0000.00.29.xx.xx` folder containing:
- `preloader_*.bin` (e.g. `preloader_k6880v1_mdot2_datacard.bin` / `FM350.F09_preloader_*.bin`)
- `loader_ext-verified*.img`
- `Scatter.xml` (partition map / scatter file)
- `OEM_OTA*.img`, `OP_OTA*.img` and other partition images
- `download_agent/…` incl. `auth_sv5.auth` if the unit is auth-locked (SLA/DAA)

> Sourcing the image: the public `29.23.06` is distributed via **Microsoft Update
> Catalog** ("Fibocom Wireless Inc. - Firmware - 3500.5003.2306.7"). If/when Photonicat
> hosts an image, it will appear under `https://dl.photonicat.com/firmware/fm350/`, and
> the web UI's hidden updater (10 clicks on the firmware-version value on `/modem`) will
> light up automatically.

---

## 2. Understand the FM350-GL boot/reset pins (M.2 Key-B)

From the **FM350-GL Hardware Guide** (FCC ID `ZMOFM350GL`), the module is M.2 Key-B,
75 pins. The pins that matter for boot control:

- **`FCPO#` (FULL_CARD_POWER_OFF#)** — must be pulled up (3.3 V or 1.8 V) for the module
  to boot; the host AP controls start-up through it. Cutting/holding this changes whether
  the SoC powers up.
- **`RESET#`** — pull **low ≥ 2 ms (10 ms recommended)** to reset; release to restart.
  PMU keeps power across a RESET#.
- **`3.3 V` supply / VBUS** — cutting main power is the simplest way to force a cold boot
  so you can catch the BROM window.

The BROM window opens for a **fraction of a second at power-on**, *before* the preloader
runs. The whole game is: **have the flash tool armed, then power the module up** so the
tool grabs the SoC at `0e8d:2000` before the preloader takes over.

> The exact boot-config/BOOT-SEL pad (the MTK "KPCOL0-equivalent" line that forces BROM
> when grounded at power-on) is **not published** in the FM350-GL guide. On most MTK M.2
> cards a simple **power-cut-at-connect** (below) is enough and you never need a test
> point. Only hunt for a boot pad if the power-cut method keeps landing you in Preloader.

---

## 3. Entering BROM — three methods, least-invasive first

### Method A — Power-cut / "arm then connect" (try this first)

1. Install drivers (Win) / udev rules (Linux). **Blacklist / stop ModemManager** so it
   doesn't grab the port: `systemctl stop ModemManager` (Linux).
2. Launch the flash tool and **arm it**:
   - *SP Flash Tool:* load the `Scatter.xml`, tick at least one partition, choose
     **Download Only**, click **Download** (green arrow). It's now waiting for a device.
   - *mtkclient:* run its BROM/crash-preloader command so it's polling for `0e8d:2000`.
3. With the module **unpowered** (USB not yet connected, or VBUS switched off), **connect
   USB / apply power**. The tool should catch the SoC as it boots.
4. Watch for `0e8d:2000`:
   - Linux: `watch -n0.1 'lsusb | grep 0e8d'`
   - Windows: Device Manager → "MediaTek USB Port / Preloader USB VCOM".

### Method B — Crash the preloader into BROM (software)

If Method A only reaches **Preloader** (`0e8d:200x`, VCOM) and not BROM, use a tool with a
**"crash preloader → BROM"** feature (`mtkclient`, MTK Meta/Auth utilities). It sends a
payload that drops the SoC from preloader back into BootROM so the tool can handshake.

### Method C — Boot-config test point (hardware, last resort)

Only if A and B fail (common on secure/OEM-locked cards where BROM is blocked in the
preloader):

1. On the M.2 adapter, identify the boot-select / boot-config pad near the SoC (short
   trace to a resistor pad). This is **not documented** for the FM350-GL — you may need to
   buzz it out or find a teardown for your board revision.
2. With the module **off**, **ground that pad** (to a GND pin), then **apply power** while
   holding it, and release once `0e8d:2000` appears.
3. Alternatively, cut **`FCPO#`** / toggle **`RESET#`** per the pin behavior in §2 to force
   a clean cold boot into the BROM window.

> **Never** short pins you haven't positively identified as the boot-config line — shorting
> the wrong pad can kill the module. If unsure, stop and get the board-specific test-point
> map first.

---

## 4. Flash

Once the tool shows the device at `0e8d:2000`:

- **SP Flash Tool → Download Only** (never "Format All + Download" unless you have a full,
  verified image incl. preloader — that wipes NVRAM/IMEI). Let it run to the green ✓.
  Do **not** unplug mid-flash.
- **Lenovo/Fibocom Linux tool:** run its updater as root; it auto-detects the FM350-GL,
  compares versions, and flashes. Use the bundled **switch tool** only if you must change
  the carrier/FCC firmware profile.
- If the unit is **auth-locked (SLA/DAA)** the tool needs the matching `auth_sv5.auth` /
  authenticated DA, or it aborts at handshake. Without it you cannot proceed — this is a
  security feature, not a bug.

After it completes, power-cycle the module and verify:

```
# on any host that can reach its AT port (e.g. the Photonicat)
AT+CGMR          → new revision, e.g. 81600.0000.00.29.23.06
AT+GTPKGVER?     → full package string
```

---

## 5. Verify from the Photonicat web UI

Put the card back in the Photonicat, then open **`http://<device>/modem#modem_basic`**.
The **Modem Firmware Version** row shows the live value. (Clicking that value **10×** opens
the hidden "Modem Firmware Update" dialog, which reports current vs. latest and, once an
image is hosted at `dl.photonicat.com/firmware/fm350/`, offers the update.)

---

## 6. Recover from a bad flash

- If the module disappears entirely (no `0e8d:xxxx` at all): retry **Method A/C** — a
  bricked MTK part usually still exposes BROM at power-on, which is exactly what you use to
  reflash a known-good image.
- Keep a **backup of the original firmware** partitions before writing anything (SP Flash
  Tool → *Readback*, or `mtkclient` dump) so you can restore `81600.0000.00.29.20.30`.

---

## References

- FM350-GL Hardware Guide (pin behavior, FCPO#/RESET#): FCC ID `ZMOFM350GL` —
  <https://fcc.report/FCC-ID/ZMOFM350GL/6551012.pdf>
- Community FM350-GL flash guide (SP Flash Tool v6.2124, file layout):
  <https://digtvbg.com/files/fibocom-fm350-gl-5g-esim/FLASH-FW.txt>
- Lenovo FM350 Linux update tool (Ubuntu `ds568880` / Fedora `ds569694`):
  <https://support.lenovo.com/us/en/downloads/ds568880-fibocom-fm350-firmware-update-and-switch-tool-for-ubuntu-linux-thinkpad>
- SP Flash Tool for Linux:
  <https://spflashtools.com/linux/sp-flash-tool-v6-2228-for-linux>
- General MTK BROM entry (methods, timing, VID:PID): ChimeraTool —
  <https://chimeratool.com/en/docs/mediatek-bootrom-mode>
- `mtkclient` (open-source MTK flash/repair, crash-to-BROM): <https://github.com/bkerler/mtkclient>
- OpenWrt FM350-GL support thread: <https://forum.openwrt.org/t/fibocom-fm350-gl-support/142682>

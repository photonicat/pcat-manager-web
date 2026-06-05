# OEM Branding & Serial Number Configuration

This document describes how OEM branding, serial numbers, and device identity work in pcat-manager-web.

## Overview

All OEM-specific data (branding, URLs, images, serial number) is stored in a single JSON blob on the eMMC boot1 partition (`/dev/mmcblk0boot1`). The serial number is cryptographically signed with Ed25519 so it cannot be forged. OEM branding data is unsigned — device owners can customize it freely.

## boot1 JSON Format

```json
{
  "machine_sn": "PCAT2-ABC123",
  "emmc_sn": "0xDEADBEEF",
  "signature": "base64-encoded-ed25519-signature",
  "oem": {
    "name": "Photonicat 2",
    "id": "photonicat",
    "company_url": "https://photonicat.com",
    "wiki_url": "https://photonicat.com/wiki/Main_Page",
    "forum_url": "https://photonicat.com/forums",
    "tips_url": "https://api.photonicat.com/tips.php",
    "firmware_base_url": "https://dl.photonicat.com/repos/photonicatwrt/photonicat2/",
    "ip_check_urls": ["https://api.ariaboard.com/ip.php", "https://4.photonicat.com/ip.php"],
    "ipv6_check_url": "https://6.photonicat.com/ip.php",
    "colors": {
      "primary": "#008AD3",
      "accent": "#FDE021",
      "dark": "#00274D"
    },
    "images": {
      "logo_svg": "PHN2ZyB4bWxucz0i...",
      "logo_text_png": "iVBORw0KGgo...",
      "favicon_ico": "AAABAA...",
      "favicon_16_png": "iVBORw0KGgo...",
      "favicon_32_png": "iVBORw0KGgo...",
      "apple_touch_icon_png": "iVBORw0KGgo..."
    }
  }
}
```

The JSON is null-terminated (`\0`) on the partition. Remaining bytes are `0xFF`.

## Serial Number Signing

### How it works

1. **Factory server** holds the Ed25519 private key (never in this repo)
2. The message `"{emmc_sn}|{machine_sn}"` is signed, producing a 64-byte signature
3. The signature is base64-encoded and stored in the `"signature"` field
4. **Device** verifies using the public key embedded in `app/oem_config.py`

### Public key

```
dsSEUtJtUy7PtByCPj40EOVF0I8RlMqgIRKaz6toQdo=
```

This is the Ed25519 public key (base64, 32 bytes). It can only **verify** signatures, not create them.

### UI behavior

| Condition | Display |
|---|---|
| Valid signature | SN with green checkmark |
| Missing/invalid signature | SN with red "(invalid)" label |
| No boot1 data | "DEV-UNKNOWN" (development) or "N/A" |

The serial number is always shown regardless of signature validity. The indicator tells the user whether the SN is factory-authentic.

## OEM Fields Reference

| Field | Required | Description |
|---|---|---|
| `machine_sn` | Yes | Device serial number displayed in UI |
| `emmc_sn` | Yes* | eMMC hardware serial (for signature verification) |
| `signature` | Yes* | Ed25519 signature over `emmc_sn\|machine_sn` |
| `oem.name` | No | Brand name shown in header and page titles |
| `oem.id` | No | Short identifier (e.g., "photonicat") |
| `oem.company_url` | No | Company website URL |
| `oem.wiki_url` | No | Documentation/wiki URL (shown in footer) |
| `oem.forum_url` | No | Community forum URL (shown in footer) |
| `oem.tips_url` | No | Tips/notification API endpoint |
| `oem.firmware_base_url` | No | Firmware package repository base URL |
| `oem.ip_check_urls` | No | List of URLs for WAN IP detection |
| `oem.ipv6_check_url` | No | URL for WAN IPv6 detection |
| `oem.colors` | No | Brand colors (primary, accent, dark) |
| `oem.images.*` | No | Base64-encoded image assets |

*Required for SN verification. Without these, the SN shows as "invalid".

## Image Keys

| Key | File | Description |
|---|---|---|
| `logo_svg` | `logo.svg` | Header logo (SVG preferred) |
| `logo_png` | `logo.png` | Header logo (PNG fallback) |
| `logo_text_png` | `logo_text.png` | Text/wordmark logo |
| `favicon_ico` | `favicon.ico` | Browser favicon |
| `favicon_16_png` | `favicon-16x16.png` | 16x16 favicon |
| `favicon_32_png` | `favicon-32x32.png` | 32x32 favicon |
| `apple_touch_icon_png` | `apple-touch-icon.png` | iOS home screen icon |

Images are served from `/oem/image/<key>` and decoded from base64 on each request (cached by browser via `Cache-Control: max-age=86400`).

## Fallback Chain

When a field is not present in boot1:

1. **boot1 JSON** `oem` section (primary source)
2. **`oem.txt` file** in the app directory (legacy fallback)
3. **Built-in defaults** (generic "Router" branding, no external URLs)

Static image files in `app/static/` (e.g., `logo.svg`, `favicon.ico`) serve as the final fallback when no OEM images are configured.

## Factory Provisioning

### Using the helper script

```bash
# On the target device during manufacturing:

# 1. Read the eMMC serial
EMMC_SN=$(cat /sys/class/block/mmcblk0/device/serial)

# 2. Generate and write boot1 (with signing)
python3 tools/write_boot1_oem.py \
  --machine-sn "PCAT2-ABC123" \
  --emmc-sn "$EMMC_SN" \
  --sign-url http://factory-server:8000/api/sign \
  --oem-name "Photonicat 2" \
  --oem-company-url "https://photonicat.com" \
  --oem-wiki-url "https://photonicat.com/wiki/Main_Page" \
  --oem-forum-url "https://photonicat.com/forums" \
  --oem-tips-url "https://api.photonicat.com/tips.php" \
  --oem-ip-check-urls "https://api.ariaboard.com/ip.php,https://4.photonicat.com/ip.php" \
  --oem-ipv6-check-url "https://6.photonicat.com/ip.php" \
  --oem-firmware-url "https://dl.photonicat.com/repos/photonicatwrt/photonicat2/" \
  --image-dir /path/to/brand/images/ \
  --write /dev/mmcblk0boot1

# 3. Verify (dry run on an already-written device)
python3 tools/write_boot1_oem.py \
  --machine-sn "PCAT2-ABC123" \
  --emmc-sn "$EMMC_SN" \
  --dry-run
```

### Testing locally

```bash
# Generate a test payload file (no signing server needed)
python3 tools/write_boot1_oem.py \
  --machine-sn "TEST-001" \
  --emmc-sn "0x12345678" \
  --oem-name "Test Brand" \
  --output test_boot1.bin

# Inspect the result
python3 -c "import json; print(json.dumps(json.loads(open('test_boot1.bin','rb').read().split(b'\x00')[0]), indent=2))"
```

## Size Budget

The eMMC boot1 partition is typically 4MB. Current usage:

| Component | Size |
|---|---|
| JSON structure + SN + URLs | ~500 bytes |
| Signature | ~90 bytes (base64) |
| `logo.svg` (base64) | ~1.1 KB |
| `logo_text.png` (base64) | ~12.6 KB |
| `favicon.ico` (base64) | ~20.8 KB |
| `favicon-16x16.png` (base64) | ~0.9 KB |
| `favicon-32x32.png` (base64) | ~2.0 KB |
| `apple-touch-icon.png` (base64) | ~31.6 KB |
| **Total** | **~70 KB** |

Well within the 4MB limit.

## Security Model

| What | Protected? | How |
|---|---|---|
| Serial number authenticity | Yes | Ed25519 signature (private key in factory only) |
| OEM branding | No | Device owner can customize freely |
| Private key exposure | Safe | Only public key is in this repo |
| Code is open source | Safe | Knowing the public key doesn't help forge signatures |

The private key (`PRIVATE_KEY` env var) lives exclusively on the factory signing server (`aeb_flask_server`). It is **never** committed to any public repository.

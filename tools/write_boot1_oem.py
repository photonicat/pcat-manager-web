#!/usr/bin/env python3
"""
Factory tool: Generate and write OEM boot1 JSON payload.

This script:
  1. Builds the boot1 JSON (machine_sn, emmc_sn, oem branding, base64 images)
  2. Optionally calls the factory signing server to get an Ed25519 signature
  3. Writes the result to /dev/mmcblk0boot1 (or a file for testing)

Usage:
  # Generate payload to stdout (for inspection)
  python3 write_boot1_oem.py --machine-sn PCAT2-ABC123 --emmc-sn 0xDEADBEEF --dry-run

  # Sign via factory server and write to boot1
  python3 write_boot1_oem.py --machine-sn PCAT2-ABC123 --emmc-sn 0xDEADBEEF \
      --sign-url http://factory-server:8000/api/sign \
      --image-dir /path/to/brand/images \
      --oem-name "Photonicat 2" \
      --oem-company-url "https://photonicat.com/wiki/Main_Page" \
      --write /dev/mmcblk0boot1

  # Write to a test file
  python3 write_boot1_oem.py --machine-sn TEST-001 --emmc-sn 0x12345678 \
      --output test_boot1.bin
"""

import argparse
import base64
import json
import os
import sys


def load_image_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_images_dict(image_dir):
    """Scan image_dir for known OEM image files and encode them."""
    if not image_dir or not os.path.isdir(image_dir):
        return {}
    mapping = {
        "logo.svg": "logo_svg",
        "logo.png": "logo_png",
        "logo_text.png": "logo_text_png",
        "favicon.ico": "favicon_ico",
        "favicon-16x16.png": "favicon_16_png",
        "favicon-32x32.png": "favicon_32_png",
        "apple-touch-icon.png": "apple_touch_icon_png",
    }
    images = {}
    for filename, key in mapping.items():
        path = os.path.join(image_dir, filename)
        if os.path.exists(path):
            images[key] = load_image_b64(path)
            print(f"  Loaded {filename} -> {key} ({os.path.getsize(path)} bytes)")
    return images


def sign_payload(sign_url, emmc_sn, machine_sn):
    """Call factory signing server to get Ed25519 signature."""
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "emmc_sn": emmc_sn,
        "machine_sn": machine_sn
    }).encode("utf-8")

    req = urllib.request.Request(
        sign_url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("status") == "okay":
                return result["signature"]
            else:
                print(f"Signing server error: {result}", file=sys.stderr)
                sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Failed to reach signing server at {sign_url}: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Generate OEM boot1 payload")
    parser.add_argument("--machine-sn", required=True, help="Machine serial number")
    parser.add_argument("--emmc-sn", required=True, help="eMMC serial (from /sys/class/block/mmcblk0/device/serial)")
    parser.add_argument("--sign-url", help="Factory signing server URL (e.g. http://host:8000/api/sign)")
    parser.add_argument("--oem-name", default="", help="OEM display name")
    parser.add_argument("--oem-id", default="", help="OEM identifier")
    parser.add_argument("--oem-company-url", default="", help="OEM company URL")
    parser.add_argument("--oem-wiki-url", default="", help="OEM wiki/docs URL")
    parser.add_argument("--oem-forum-url", default="", help="OEM forum/community URL")
    parser.add_argument("--oem-tips-url", default="", help="OEM tips API URL")
    parser.add_argument("--oem-firmware-url", default="", help="OEM firmware base URL")
    parser.add_argument("--oem-ip-check-urls", default="", help="Comma-separated IP check URLs")
    parser.add_argument("--oem-ipv6-check-url", default="", help="IPv6 check URL")
    parser.add_argument("--image-dir", help="Directory containing OEM image files")
    parser.add_argument("--output", help="Write payload to this file (for testing)")
    parser.add_argument("--write", help="Write payload to block device (e.g. /dev/mmcblk0boot1)")
    parser.add_argument("--dry-run", action="store_true", help="Print payload JSON to stdout, don't write")

    args = parser.parse_args()

    # Build OEM section
    oem = {}
    if args.oem_name:
        oem["name"] = args.oem_name
    if args.oem_id:
        oem["id"] = args.oem_id
    if args.oem_company_url:
        oem["company_url"] = args.oem_company_url
    if args.oem_wiki_url:
        oem["wiki_url"] = args.oem_wiki_url
    if args.oem_forum_url:
        oem["forum_url"] = args.oem_forum_url
    if args.oem_tips_url:
        oem["tips_url"] = args.oem_tips_url
    if args.oem_firmware_url:
        oem["firmware_base_url"] = args.oem_firmware_url
    if args.oem_ip_check_urls:
        oem["ip_check_urls"] = [u.strip() for u in args.oem_ip_check_urls.split(",")]
    if args.oem_ipv6_check_url:
        oem["ipv6_check_url"] = args.oem_ipv6_check_url

    # Load images
    print("Loading OEM images...")
    images = build_images_dict(args.image_dir)
    if images:
        oem["images"] = images

    # Get signature
    signature = ""
    if args.sign_url:
        print(f"Requesting signature from {args.sign_url}...")
        signature = sign_payload(args.sign_url, args.emmc_sn, args.machine_sn)
        print(f"  Signature: {signature[:32]}...")
    else:
        print("WARNING: No --sign-url provided. SN will show as unverified on device.")

    # Build final payload
    boot1 = {
        "machine_sn": args.machine_sn,
        "emmc_sn": args.emmc_sn,
        "signature": signature,
    }
    if oem:
        boot1["oem"] = oem

    payload_json = json.dumps(boot1, separators=(",", ":"))
    payload_bytes = payload_json.encode("utf-8") + b"\x00"

    print(f"\nPayload size: {len(payload_bytes)} bytes ({len(payload_bytes)/1024:.1f} KB)")

    if args.dry_run:
        print("\n--- Payload JSON ---")
        print(json.dumps(boot1, indent=2))
        return

    if args.output:
        with open(args.output, "wb") as f:
            f.write(payload_bytes)
        print(f"Written to {args.output}")

    if args.write:
        # For eMMC boot partitions, we need to disable read-only first
        boot_dev = args.write
        boot_name = os.path.basename(boot_dev)
        force_ro_path = f"/sys/block/{boot_name.replace('boot0','mmcblk0').replace('boot1','mmcblk0')}/force_ro"
        # Try the boot-partition-specific path
        boot_ro_path = f"/sys/block/mmcblk0/{boot_name}/force_ro"

        for ro_path in [boot_ro_path, force_ro_path]:
            if os.path.exists(ro_path):
                print(f"Disabling read-only: {ro_path}")
                with open(ro_path, "w") as f:
                    f.write("0")
                break

        with open(boot_dev, "wb") as f:
            f.write(payload_bytes)
        print(f"Written to {boot_dev}")

        # Re-enable read-only
        for ro_path in [boot_ro_path, force_ro_path]:
            if os.path.exists(ro_path):
                with open(ro_path, "w") as f:
                    f.write("1")
                print(f"Re-enabled read-only: {ro_path}")
                break


if __name__ == "__main__":
    main()

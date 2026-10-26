#!/usr/bin/env python3
"""
Yeelight Liberator & Developer Mode Unlocker
===========================================
Universal offline tool to rescue, unlock, and provision "obsolete" factory-reset
Yeelight Smart Bulbs without vendor cloud, Xiaomi accounts, or proprietary apps.

Exploit & Mechanism:
--------------------
1. When in factory reset AP mode (yeelink-light-*), the bulb broadcasts an open Wi-Fi network.
2. It responds to UDP port 54321 with an unmasked 16-byte handshake session token.
3. We inject `dev.send("set_ps", ["cfg_lan_ctrl", "1"])` directly into persistent NVRAM flash memory.
4. We provision home Wi-Fi credentials via `miIO.config_router`.
5. Once associated with your home router, TCP port 55443 remains permanently open for local LAN control.
"""

import argparse
import getpass
import json
import logging
import os
import platform
import re
import socket
import subprocess
import sys
import time
import warnings

warnings.filterwarnings("ignore")

try:
    from miio import Device
    from miio.exceptions import DeviceException
except ImportError:
    print("\n[!] Error: 'python-miio' is required. Please install dependencies:")
    print("    pip install -r requirements.txt\n")
    sys.exit(1)

# ANSI Colors for terminal output
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"

DEFAULT_BULB_IPS = ["192.168.10.1", "192.168.4.1"]
SSDP_ADDR = "239.255.250.250"
SSDP_PORT = 1982
SSDP_MSG = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.250.250:1982\r\n"
    'MAN: "ssdp:discover"\r\n'
    "ST: wifi_bulb\r\n\r\n"
)


def log_step(msg: str):
    print(f"\n{BOLD}{CYAN}==>{RESET} {BOLD}{msg}{RESET}")


def log_success(msg: str):
    print(f"{BOLD}{GREEN}[✓]{RESET} {msg}")


def log_info(msg: str):
    print(f"{CYAN}[i]{RESET} {msg}")


def log_warn(msg: str):
    print(f"{YELLOW}[!] WARNING:{RESET} {msg}")


def log_error(msg: str):
    print(f"{RED}[✗] ERROR:{RESET} {msg}")


def run_cmd(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return res.returncode, res.stdout.strip()
    except subprocess.TimeoutExpired:
        return -1, "Command timed out"
    except FileNotFoundError:
        return 127, f"Command not found: {cmd[0]}"
    except Exception as e:
        return 1, str(e)


def has_nmcli() -> bool:
    code, _ = run_cmd(["which", "nmcli"], timeout=5)
    return code == 0


def scan_wifi_networks() -> list[dict[str, str]]:
    """Scan available Wi-Fi networks using nmcli."""
    run_cmd(["nmcli", "dev", "wifi", "rescan"], timeout=10)
    time.sleep(1.5)
    code, out = run_cmd(["nmcli", "-t", "-f", "SSID,BSSID,CHAN,SIGNAL,SECURITY", "dev", "wifi", "list"], timeout=10)
    if code != 0:
        return []

    networks = []
    seen = set()
    for line in out.splitlines():
        if not line or line.startswith("\\:"):
            continue
        parts = line.split(":")
        if len(parts) >= 2:
            ssid = parts[0]
            bssid = ":".join(parts[1:7]) if len(parts) >= 7 else ""
            if ssid and ssid not in seen:
                seen.add(ssid)
                networks.append({"ssid": ssid, "bssid": bssid})
    return networks


def find_yeelight_aps() -> list[dict[str, str]]:
    """Find Wi-Fi APs matching Yeelight factory patterns."""
    networks = scan_wifi_networks()
    yeelight_aps = []
    pattern = re.compile(r"^(yeelink-light-|yeelight_|yeelink_)", re.IGNORECASE)
    for net in networks:
        if pattern.search(net["ssid"]):
            yeelight_aps.append(net)
    return yeelight_aps


def get_current_wifi_connection() -> str | None:
    """Detect current active Wi-Fi connection profile."""
    code, out = run_cmd(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"], timeout=5)
    if code == 0:
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and ("wireless" in parts[1] or "wifi" in parts[1]):
                return parts[0]
    return None


def get_handshake_token(bulb_ip: str, timeout: float = 3.0) -> tuple[str, int, int] | None:
    """
    Send miIO UDP 54321 hello packet and extract dynamic handshake token.
    Token is located in bytes 16:32 of the 32-byte response.
    """
    hello_packet = bytes.fromhex("21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(hello_packet, (bulb_ip, 54321))
        data, _ = sock.recvfrom(1024)
        if len(data) >= 32 and data[0:2] == bytes.fromhex("2131"):
            dev_id = int.from_bytes(data[8:12], byteorder="big")
            ts = int.from_bytes(data[12:16], byteorder="big")
            token_hex = data[16:32].hex()
            return token_hex, dev_id, ts
    except Exception:
        return None
    finally:
        sock.close()


def detect_bulb_ip() -> str | None:
    """Probe common Yeelight AP gateway IPs."""
    for ip in DEFAULT_BULB_IPS:
        token_info = get_handshake_token(ip, timeout=1.5)
        if token_info:
            return ip
    return None


def unlock_and_provision(bulb_ip: str, target_ssid: str, target_password: str) -> bool:
    """
    Perform the NVRAM unlock and push home Wi-Fi credentials.
    """
    log_info(f"Initiating UDP handshake with bulb gateway at {bulb_ip}:54321...")
    token_info = None
    for attempt in range(1, 4):
        token_info = get_handshake_token(bulb_ip, timeout=2.5)
        if token_info:
            break
        time.sleep(1)

    if not token_info:
        log_error(f"Bulb at {bulb_ip} did not respond to UDP handshake.")
        return False

    token, dev_id, ts = token_info
    log_success(f"Handshake successful! Device ID: {hex(dev_id)}, Session Token: {token[:6]}...{token[-4:]}")

    if token.lower() == "ffffffffffffffffffffffffffffffff":
        log_error("Token returned as masked 'ffff...'. The bulb must be in factory AP mode to be unlocked.")
        return False

    try:
        dev = Device(ip=bulb_ip, token=token, timeout=8)

        # 1. PERMANENT NVRAM INJECTION
        log_step("Injecting Developer Mode / LAN Control into NVRAM Flash (cfg_lan_ctrl: 1)...")
        nvram_ok = False
        for attempt in range(1, 4):
            try:
                res = dev.send("set_ps", ["cfg_lan_ctrl", "1"])
                if res == ["ok"] or res == "ok":
                    nvram_ok = True
                    log_success(f"Developer Mode successfully written to NVRAM flash! Response: {res}")
                    break
            except Exception as e:
                log_warn(f"set_ps attempt {attempt} failed: {e}. Retrying...")
                time.sleep(1)

        if not nvram_ok:
            log_warn("Could not confirm 'ok' from set_ps, attempting Wi-Fi configuration anyway...")

        # 2. PROVISION ROUTER CREDENTIALS
        log_step(f"Sending Wi-Fi credentials for SSID '{target_ssid}'...")
        params = {"ssid": target_ssid, "passwd": target_password, "uid": 0}
        try:
            res_wifi = dev.send("miIO.config_router", params)
            log_success(f"Bulb acknowledged Wi-Fi configuration! Response: {res_wifi}")
        except DeviceException:
            # When the bulb switches Wi-Fi, it often drops the connection immediately
            log_success("Wi-Fi credentials dispatched! Bulb is now associating with your home router...")

        return True

    except Exception as e:
        log_error(f"Error communicating with bulb: {e}")
        return False


def discover_on_lan(timeout: int = 15) -> list[dict[str, str]]:
    """Discover Yeelight bulbs on local network via SSDP multicast."""
    discovered = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(2.0)

    start_time = time.time()
    seen_ips = set()

    while time.time() - start_time < timeout:
        try:
            sock.sendto(SSDP_MSG.encode("utf-8"), (SSDP_ADDR, SSDP_PORT))
        except Exception:
            pass

        loop_end = time.time() + 2.0
        while time.time() < loop_end:
            try:
                data, addr = sock.recvfrom(2048)
                ip = addr[0]
                if ip not in seen_ips:
                    seen_ips.add(ip)
                    text = data.decode("utf-8", errors="ignore")
                    bulb_info = {"ip": ip, "raw": text}
                    for line in text.split("\r\n"):
                        if ":" in line:
                            k, v = line.split(":", 1)
                            bulb_info[k.strip().lower()] = v.strip()
                    discovered.append(bulb_info)
            except socket.timeout:
                break
            except Exception:
                pass

        if discovered:
            break

    sock.close()
    return discovered


def test_lan_control(bulb_ip: str, port: int = 55443) -> bool:
    """Test TCP port 55443 JSON-RPC connection directly."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(4.0)
    try:
        sock.connect((bulb_ip, port))
        cmd = json.dumps({"id": 1, "method": "get_prop", "params": ["power", "bright", "rgb", "name"]}) + "\r\n"
        sock.sendall(cmd.encode("utf-8"))
        res = sock.recv(1024).decode("utf-8")
        parsed = json.loads(res)
        if "result" in parsed:
            log_success(f"Direct TCP socket (Port {port}) responded: {parsed['result']}")
            return True
        return False
    except Exception as e:
        log_warn(f"Could not connect to {bulb_ip}:{port}: {e}")
        return False
    finally:
        sock.close()


def flash_test_bulb(bulb_ip: str, port: int = 55443):
    """Blink bulb twice to physically demonstrate local control."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(4.0)
    try:
        sock.connect((bulb_ip, port))
        # Turn on
        sock.sendall((json.dumps({"id": 10, "method": "set_power", "params": ["on", "smooth", 300]}) + "\r\n").encode())
        time.sleep(0.4)
        # Pulse Emerald Green
        sock.sendall((json.dumps({"id": 11, "method": "set_rgb", "params": [65280, "smooth", 300]}) + "\r\n").encode())
        time.sleep(0.5)
        # Pulse Warm White
        sock.sendall((json.dumps({"id": 12, "method": "set_ct_abx", "params": [3500, "smooth", 300]}) + "\r\n").encode())
    except Exception:
        pass
    finally:
        sock.close()


def print_banner():
    banner = f"""
{BOLD}{MAGENTA}======================================================================
     YEELIGHT LIBERATOR & DEVELOPER MODE PERMANENT UNLOCKER
======================================================================{RESET}
  {BOLD}Zero Cloud • Zero Xiaomi Accounts • 100% Local LAN Control{RESET}
  Compatible with: Color 1/2/3, White/Mono, Lightstrips, Ceiling
"""
    print(banner)


def interactive_wizard():
    print_banner()

    nmcli_available = has_nmcli()
    log_info(f"System: {platform.system()} ({platform.release()})")
    log_info(f"NetworkManager (nmcli): {'Available (Automatic mode)' if nmcli_available else 'Not available (Manual mode)'}")

    print("\n" + BOLD + "BEFORE YOU START:" + RESET)
    print("  1. Screw in your Yeelight bulb into a lamp.")
    print("  2. If not in pairing mode, factory reset it:")
    print("     -> Turn the light switch OFF and ON 5 times consecutively (2s ON, 2s OFF).")
    print("     -> The bulb will cycle colors and flash white, then reboot.")
    print("  3. It will broadcast an open Wi-Fi network like 'yeelink-light-colorX_miapXXXX'.\n")

    input(f"Press {BOLD}[Enter]{RESET} when your bulb is powered on and flashing...")

    # Step 1: Detect / Select Bulb AP
    target_ap = None
    if nmcli_available:
        log_step("Scanning for Yeelight pairing networks...")
        yeelight_aps = find_yeelight_aps()
        if yeelight_aps:
            log_success(f"Found {len(yeelight_aps)} Yeelight bulb(s) in pairing mode:")
            for idx, ap in enumerate(yeelight_aps, 1):
                print(f"  [{idx}] {BOLD}{ap['ssid']}{RESET} (BSSID: {ap['bssid']})")
            if len(yeelight_aps) == 1:
                target_ap = yeelight_aps[0]["ssid"]
                log_info(f"Auto-selected: {target_ap}")
            else:
                choice = input(f"\nSelect bulb [1-{len(yeelight_aps)}]: ").strip()
                target_ap = yeelight_aps[int(choice) - 1]["ssid"]
        else:
            log_warn("No 'yeelink-light-*' networks found automatically.")
            target_ap = input("Enter bulb AP SSID manually (or press Enter to retry scan): ").strip()
            if not target_ap:
                return interactive_wizard()
    else:
        target_ap = input("Enter bulb AP SSID (e.g. yeelink-light-color2_miap18E5): ").strip()

    # Step 2: Target Home Wi-Fi Details
    log_step("Configuring target Home Wi-Fi...")
    orig_connection = get_current_wifi_connection() if nmcli_available else None
    home_ssid = ""
    if orig_connection:
        log_info(f"Detected current home Wi-Fi profile: '{orig_connection}'")
        use_curr = input(f"Connect the bulb to '{orig_connection}'? [Y/n]: ").strip().lower()
        if use_curr in ("", "y", "yes"):
            home_ssid = orig_connection

    if not home_ssid:
        home_ssid = input("Enter your 2.4 GHz Home Wi-Fi SSID: ").strip()

    home_password = getpass.getpass(f"Enter password for '{home_ssid}': ")

    # Step 3: Wi-Fi Association
    if nmcli_available:
        log_step(f"Connecting computer Wi-Fi to bulb AP '{target_ap}'...")
        code, out = run_cmd(["nmcli", "dev", "wifi", "connect", target_ap], timeout=25)
        if code != 0:
            log_warn(f"Could not connect automatically via nmcli ({out}).")
            input(f"Please manually connect your computer's Wi-Fi to '{target_ap}', then press [Enter]...")
        else:
            log_success(f"Connected to '{target_ap}'.")
            time.sleep(2)
    else:
        input(f"\nPlease connect your computer/phone Wi-Fi to '{target_ap}', then press [Enter]...")

    # Step 4: Detect Gateway IP and Execute Exploit
    bulb_ip = detect_bulb_ip() or "192.168.10.1"
    log_info(f"Using bulb gateway IP: {bulb_ip}")

    success = unlock_and_provision(bulb_ip, home_ssid, home_password)

    # Step 5: Restore Home Wi-Fi
    if nmcli_available and orig_connection:
        log_step(f"Restoring computer connection to '{orig_connection}'...")
        run_cmd(["nmcli", "connection", "up", orig_connection], timeout=20)
        time.sleep(3)
    else:
        input(f"\nPlease reconnect your computer Wi-Fi back to '{home_ssid}', then press [Enter]...")

    if not success:
        log_error("Unlocking/provisioning step encountered errors. Review messages above.")
        return

    # Step 6: Post-Provisioning LAN Discovery & Verification
    log_step("Scanning home network for the newly liberated bulb (SSDP Multicast)...")
    time.sleep(3)
    discovered = discover_on_lan(timeout=12)

    new_bulb_ip = None
    if discovered:
        for b in discovered:
            log_success(f"Found bulb at LAN IP: {BOLD}{b['ip']}{RESET} (Model: {b.get('model', 'unknown')}, ID: {b.get('id', 'unknown')})")
            new_bulb_ip = b["ip"]
    else:
        log_warn("SSDP broadcast did not find the bulb immediately (some routers block multicast).")
        custom_ip = input("Check your router's DHCP client list. Enter assigned bulb IP if known (or Enter to skip): ").strip()
        if custom_ip:
            new_bulb_ip = custom_ip

    if new_bulb_ip:
        log_step(f"Testing permanent LAN Mode on {new_bulb_ip}:55443...")
        if test_lan_control(new_bulb_ip):
            log_success(f"{BOLD}SUCCESS: Bulb is permanently unlocked and responsive over local TCP port 55443!{RESET}")
            flash_test_bulb(new_bulb_ip)
            print("\n" + BOLD + GREEN + "======================================================================")
            print("                 LIBERATION COMPLETED SUCCESSFULLY!                   ")
            print("======================================================================" + RESET)
            print(f"\nDevice Summary:")
            print(f"  • IP Address : {BOLD}{new_bulb_ip}{RESET}")
            print(f"  • Port       : 55443 (JSON-RPC)")
            print(f"  • Mode       : Developer Mode / LAN Control (Permanent NVRAM)")
            print(f"\nHow to control it:")
            print(f"  1. {BOLD}Home Assistant{RESET}:")
            print(f"     Go to Settings -> Devices & Services -> Add Integration -> Yeelight -> Host: {new_bulb_ip}")
            print(f"  2. {BOLD}Router Tip{RESET}:")
            print(f"     Set a static DHCP reservation for this bulb so its IP never changes.")
        else:
            log_warn("Port 55443 did not respond yet. It might still be booting or obtaining DHCP lease.")
    else:
        print("\n" + BOLD + "Next steps:" + RESET)
        print("  1. Check your router's DHCP leases to find the bulb's assigned IP.")
        print(f"  2. Test with: python liberate.py --test-only <BULB_IP>")


def main():
    parser = argparse.ArgumentParser(description="Yeelight Liberator & Developer Mode Unlocker")
    parser.add_argument("--test-only", metavar="IP", help="Directly test port 55443 on an already-provisioned bulb IP")
    parser.add_argument("--bulb-ip", default=None, help="Bulb AP gateway IP (default: auto 192.168.10.1 / 192.168.4.1)")
    parser.add_argument("--bulb-ssid", help="Specific bulb AP SSID")
    parser.add_argument("--ssid", help="Home Wi-Fi SSID")
    parser.add_argument("--password", help="Home Wi-Fi password")
    parser.add_argument("--manual", action="store_true", help="Force manual Wi-Fi switching")
    args = parser.parse_args()

    if args.test_only:
        print_banner()
        log_step(f"Testing direct LAN Control on {args.test_only}:55443...")
        if test_lan_control(args.test_only):
            flash_test_bulb(args.test_only)
            log_success("Bulb responded and flashed! LAN Control is operational.")
        else:
            log_error("Could not reach bulb on port 55443.")
        sys.exit(0)

    # If all automated args provided, run non-interactively
    if args.ssid and args.password and args.bulb_ssid:
        print_banner()
        if not args.manual and has_nmcli():
            run_cmd(["nmcli", "dev", "wifi", "connect", args.bulb_ssid], timeout=25)
        bulb_ip = args.bulb_ip or detect_bulb_ip() or "192.168.10.1"
        ok = unlock_and_provision(bulb_ip, args.ssid, args.password)
        if ok and not args.manual and has_nmcli():
            run_cmd(["nmcli", "connection", "up", args.ssid], timeout=20)
        sys.exit(0 if ok else 1)

    interactive_wizard()


if __name__ == "__main__":
    main()

# Yeelight Liberator 💡🔓

Universal, 100% offline tool to rescue, unlock, and provision "obsolete" factory-reset Yeelight Smart LED Bulbs without vendor cloud, Xiaomi accounts, or proprietary apps.

Permanent Developer Mode (LAN Control on TCP port `55443`) is burned directly into the bulb's flash memory, making it immediately controllable via **Home Assistant**, **Homebridge**, or raw local sockets.

---

## The Problem

When older Yeelight smart bulbs (e.g. Color 1st/2nd Gen `YLDP06YL`, White/Mono `YLDP05YL`, Bedside Lamp, Lightstrips) are factory reset, the official mobile apps (Mi Home / Yeelight) may refuse to pair them or report them as "obsolete / unsupported", turning perfectly good hardware into electronic waste.

Furthermore:
- In factory AP mode, the bulb broadcasts an open Wi-Fi network (`yeelink-light-*`).
- If you simply configure Wi-Fi using standard tools, the bulb associates to your router, but **TCP Port 55443 (LAN Control) remains permanently closed by default**.
- Once associated with the router, the bulb's firmware **masks its UDP token** (returning all `0xFF`), locking you out forever without vendor cloud keys.

---

## The Solution (NVRAM Exploit)

This tool intercepts the bulb while in factory AP mode before Wi-Fi association:
1. **Token Extraction**: Sends a 32-byte UDP packet to `192.168.10.1:54321` and grabs the unmasked dynamic session token from bytes 16–31.
2. **NVRAM Flash Injection**: Injects `dev.send("set_ps", ["cfg_lan_ctrl", "1"])` into persistent SPI flash memory. This permanently forces Developer Mode to active.
3. **Router Provisioning**: Sends `miIO.config_router` with your 2.4 GHz home Wi-Fi credentials.
4. **Permanent Offline Liberation**: When the bulb boots on your home network, **TCP port 55443 remains permanently open**. You can control power, RGB, color temperature, and brightness directly on your local network.

```
+-----------------------------------------------------------------------------------------+
|                                    FACTORY AP MODE                                      |
|                                                                                         |
|  [Yeelight Bulb]                                                 [Computer / Mobile]    |
|   SSID: yeelink-light-colorX_miapXXXX                                                   |
|   Gateway: 192.168.10.1:54321                                                           |
|             |                                                             |             |
|             | <--- UDP 54321 Handshake Hello ---------------------------- |             |
|             | --- Returns unmasked token in bytes 16-31 ----------------> |             |
|             |                                                             |             |
|             | <--- dev.send("set_ps", ["cfg_lan_ctrl", "1"]) ------------- | (NVRAM Lock)|
|             | --- Acknowledges ['ok'] ----------------------------------> |             |
|             |                                                             |             |
|             | <--- dev.send("miIO.config_router", {ssid, passwd}) -------- |             |
|             v                                                             v             |
+-----------------------------------------------------------------------------------------+
                                             |
                                  Reboots & Connects to Router
                                             v
+-----------------------------------------------------------------------------------------+
|                                     HOME LAN (WIFI)                                     |
|                                                                                         |
|  [Yeelight Bulb]                                                 [Home Assistant / LAN] |
|   Home IP (e.g. 192.168.0.17)                                     http://homeassistant  |
|             |                                                             |             |
|             | <=== Direct JSON-RPC (Port 55443) ========================= |             |
|             |      NO CLOUD • NO ACCOUNTS • ZERO LATENCY                  |             |
+-----------------------------------------------------------------------------------------+
```

---

## Requirements

- Python 3.8+
- Wi-Fi adapter (Linux with `nmcli` for automated wizard, or any OS for manual mode)
- Dependencies:
  ```bash
  pip install -r requirements.txt
  ```

---

## Quick Start (Interactive Wizard)

1. **Factory reset your bulb**:
   - Turn the physical power switch **OFF and ON 5 times consecutively** (2 seconds ON, 2 seconds OFF).
   - The bulb will cycle through colors, flash white, and reboot.
   - It will now broadcast an open Wi-Fi network like `yeelink-light-color2_miap18E5`.

2. **Run the liberator**:
   ```bash
   python liberate.py
   ```

3. **Follow the on-screen prompts**:
   - On **Linux (NetworkManager)**: The script automatically scans for Yeelight APs, connects, injects the unlock, pushes home Wi-Fi credentials, reconnects your PC, discovers the bulb's new IP via SSDP, and flashes it with a green test pulse to confirm success.
   - On **macOS / Windows**: The script guides you to connect your Wi-Fi to the bulb AP, unlocks it, and guides you back.

---

## CLI Options

```bash
# Test an already unlocked bulb on your home network
python liberate.py --test-only 192.168.0.17

# Force manual Wi-Fi switching mode (recommended on macOS / Windows)
python liberate.py --manual

# Fully automated non-interactive execution
python liberate.py --bulb-ssid "yeelink-light-color2_miap18E5" --ssid "MyHomeWiFi" --password "SecretKey123"
```

---

## Controlling Your Liberated Bulbs

### 1. Home Assistant (Recommended)
Because Developer Mode is permanently active, Home Assistant discovers the bulbs natively without any custom add-ons or cloud accounts:
1. Open Home Assistant (`http://homeassistant.local:8123`).
2. Go to **Settings** -> **Devices & Services** -> **Add Integration**.
3. Search for **Yeelight**.
4. Enter the bulb's LAN IP address (e.g. `192.168.0.17`).
5. Done! Full RGB, color temperature, and brightness control are now available.

> [!TIP]
> **Assign Static DHCP Leases**: Set a static DHCP reservation in your home router for each bulb's MAC address so their IP addresses never change when the router reboots.

### 2. Direct TCP Socket Control (Raw JSON-RPC)
You can control the bulb directly from any terminal or script using TCP port `55443`:

```bash
# Turn On
echo '{"id":1,"method":"set_power","params":["on","smooth",500]}' | nc -w 1 192.168.0.17 55443

# Turn Off
echo '{"id":1,"method":"set_power","params":["off","smooth",500]}' | nc -w 1 192.168.0.17 55443

# Set RGB (Emerald Green: #00FF80 = 65408)
echo '{"id":1,"method":"set_rgb","params":[65408,"smooth",500]}' | nc -w 1 192.168.0.17 55443

# Set Color Temperature (3500K)
echo '{"id":1,"method":"set_ct_abx","params":[3500,"smooth",500]}' | nc -w 1 192.168.0.17 55443
```

---

## Hardware Safety Guidelines

To ensure the hardware lasts for years without premature failure:
1. **Respect Firmware Quota (60 req/min)**: The ESP8266 firmware has a rate limit of 60 commands per minute. Avoid aggressive rapid-fire loops in your automations to prevent dropped connections.
2. **Flash Memory Endurance**: Never call `set_default` (save state as startup default) in automated loops. It writes to physical SPI Flash memory which has finite write cycles (~10,000–100,000).
3. **PWM Driver Bounds**:
   - Brightness is clamped strictly between `1` and `100` (`0` is invalid in Yeelight protocol).
   - Color temperature is hardware-limited between `1700K` (warm candlelight) and `6500K` (cool daylight).
   - Keep transition durations >= `30ms` to protect the LED driver electronics from voltage ringing.

---

## License

MIT License. Free and open source to keep electronics out of landfills.

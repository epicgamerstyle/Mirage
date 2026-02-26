#!/usr/bin/env python3
"""
BlueStacks Air Instance Identity Randomizer (Luke's Mirage — Mac)

Goal:
- After cloning a "golden" instance (disk copy), reset per-clone persistent identifiers
  stored at /data/adb/jorkspoofer/identifiers.conf inside the Android guest.
- Optionally randomize the device profile (GPU, build fingerprint, model, etc.)
  so each instance looks like a different physical phone.

How it works:
- Reads BlueStacks Air's bluestacks.conf to discover instance names + adb ports.
- Probes which instances are actually booted (sys.boot_completed=1) via ADB.
- Lets you select instances (by name, port, or interactive list).
- For each selected instance:
  1. (if --randomize-profile) picks a random device profile from the Magisk module's
     profiles/ directory and sets it as active.conf
  2. Deletes /data/adb/jorkspoofer/identifiers.conf and regenerates it with fresh
     random values (IMEI, serial, Android ID, MAC, etc.)
  3. (unless --no-reboot) reboots the instance so all modules pick up the new config

This script is for macOS only (BlueStacks Air). For Windows BlueStacks 5, use the
Windows version in the parent directory.

Flags:
  --bluestacks-dir DIR   BlueStacks Air app contents (for hd-adb).
  --conf PATH            Path to bluestacks.conf (auto-detected if omitted).
  --adb PATH             Override path to adb executable.
  --host HOST            ADB host IP (default: 127.0.0.1).
  --list                 List all discovered instances with their status and exit.
  --running              Only target instances that are currently booted.
  --select NAME [NAME..] Target specific instances by name.
  --ports PORT [PORT..] Target specific instances by ADB port number.
  --randomize-profile    Also randomize the device profile.
  --no-reboot            Skip rebooting after reset.
  --non-interactive      Do not prompt for instance selection.

Examples:
  python randomize_instances.py --running --randomize-profile
  python randomize_instances.py --ports 5555 5575 --no-reboot
  python randomize_instances.py --list
"""

from __future__ import annotations

import argparse
import os
import random
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BLUESTACKS_DIR = "/Applications/BlueStacks.app/Contents"
DEFAULT_ENGINE_DIR = "/Users/Shared/Library/Application Support/BlueStacks/Engine"
DEFAULT_DATA_DIR = "/Users/Shared/Library/Application Support/BlueStacks"
DEFAULT_HOST = "127.0.0.1"


@dataclass(frozen=True)
class InstanceInfo:
    name: str
    adb_port: int
    display_name: str | None


def detect_engine_dir() -> Path | None:
    """Auto-detect engine directory on macOS."""
    engine = Path(DEFAULT_ENGINE_DIR)
    if engine.is_dir():
        return engine
    return None


def find_adb_exe(bluestacks_dir: str) -> str:
    """Prefer BlueStacks Air's hd-adb; fall back to 'adb' on PATH."""
    hd_adb = Path(bluestacks_dir) / "MacOS" / "hd-adb"
    if hd_adb.is_file():
        return str(hd_adb)
    return "adb"


def parse_bluestacks_conf(conf_path: Path) -> list[tuple[str, str | None]]:
    """Parse bluestacks.conf into (key, value) tuples."""
    lines: list[tuple[str, str | None]] = []
    with open(conf_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n\r")
            if "=" in line:
                k, _, v = line.partition("=")
                lines.append((k, v))
            else:
                lines.append((line, None))
    return lines


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def discover_instances_from_conf(conf_path: Path) -> list[InstanceInfo]:
    lines = parse_bluestacks_conf(conf_path)
    inst: dict[str, dict[str, str]] = {}

    pat = re.compile(r"^bst\.instance\.([^.]+)\.(.+)$")
    for k, v in lines:
        if v is None:
            continue
        m = pat.match(k)
        if not m:
            continue
        name, suffix = m.group(1), m.group(2)
        inst.setdefault(name, {})[suffix] = _strip_quotes(v)

    out: list[InstanceInfo] = []
    for name, kv in inst.items():
        if "adb_port" not in kv:
            continue
        try:
            port = int(kv["adb_port"])
        except ValueError:
            continue
        out.append(InstanceInfo(name=name, adb_port=port, display_name=kv.get("display_name")))

    out.sort(key=lambda x: x.adb_port)
    return out


def run_cmd(cmd: list[str], timeout_sec: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="timeout")


def adb_connect(adb_exe: str, serial: str, timeout_sec: int = 8) -> bool:
    cp = run_cmd([adb_exe, "connect", serial], timeout_sec=timeout_sec)
    if cp.returncode != 0:
        return False
    return "connected to" in (cp.stdout + cp.stderr).lower()


def adb_shell(adb_exe: str, serial: str, shell_cmd: str, timeout_sec: int = 10) -> subprocess.CompletedProcess:
    return run_cmd([adb_exe, "-s", serial, "shell", shell_cmd], timeout_sec=timeout_sec)


def probe_boot_completed(adb_exe: str, serial: str) -> bool:
    cp = adb_shell(adb_exe, serial, "getprop sys.boot_completed", timeout_sec=6)
    if cp.returncode != 0:
        return False
    return cp.stdout.strip() == "1"


def probe_root(adb_exe: str, serial: str) -> bool:
    cp = adb_shell(adb_exe, serial, "su -c id", timeout_sec=6)
    if cp.returncode != 0:
        return False
    return "uid=0" in cp.stdout


PROFILES_DIR = "/data/adb/modules/jorkspoofer/profiles"
ACTIVE_CONF = f"{PROFILES_DIR}/active.conf"
READABLE_ACTIVE_CONF = "/data/adb/jorkspoofer/active.conf"

# ── MCC/MNC database for carrier-country alignment ──
# Maps ISO country code → list of (carrier_name, MCC+MNC).
# Used to patch IMSI/ICCID after generation so they match PROFILE_CARRIER_COUNTRY.
# This MUST stay in sync with cdp_injector.py CARRIER_DB.
_CARRIER_MCC_MNC: dict[str, list[str]] = {
    "US": ["310260", "310410", "311480"],
    "GB": ["23430", "23415", "23420", "23410"],
    "DE": ["26201", "26202", "26207"],
    "FR": ["20801", "20810", "20820", "20815"],
    "NL": ["20408", "20416", "20404"],
    "ES": ["21407", "21401", "21403"],
    "IT": ["22201", "22210", "22288"],
    "CA": ["302720", "302610", "302220"],
    "AU": ["50501", "50502", "50503"],
    "BR": ["72405", "72406", "72404"],
    "IN": ["40586", "40410", "40411"],
    "JP": ["44010", "44020", "44050"],
    "KR": ["45005", "45008", "45006"],
    "SG": ["52501", "52505", "52503"],
    "MX": ["334020", "334050"],
    "PL": ["26003", "26006", "26002"],
    "SE": ["24001", "24007"],
    "NO": ["24201", "24202"],
    "DK": ["23801", "23802"],
    "FI": ["24405", "24491"],
    "CH": ["22801", "22802"],
    "AT": ["23201", "23203"],
    "BE": ["20601", "20610"],
    "PT": ["26806", "26803"],
    "IE": ["27205", "27201"],
    "NZ": ["53005", "53001"],
    "ZA": ["65501", "65510"],
    "AE": ["42402", "42403"],
    "TR": ["28601", "28602"],
    "RU": ["25001", "25002", "25099"],
    "TH": ["52001", "52005"],
    "PH": ["51502", "51503"],
    "ID": ["51010", "51011"],
    "MY": ["50212", "50213"],
    "VN": ["45204", "45201"],
    "CL": ["73001", "73002"],
    "CO": ["732101", "732123"],
    "AR": ["722310", "722340"],
    "EG": ["60202", "60201"],
    "NG": ["62130", "62120"],
    "KE": ["63902", "63905"],
    "IL": ["42502", "42501"],
    "SA": ["42001", "42003"],
    "PK": ["41001", "41006"],
    "BD": ["47001", "47002"],
    "TW": ["46692", "46697"],
    "HK": ["45403", "45400"],
    "RO": ["22601", "22610"],
    "CZ": ["23002", "23001"],
    "HU": ["21630", "21670"],
    "GR": ["20201", "20205"],
}

# ── Country → timezone mapping ──
# Maps ISO country code → primary IANA timezone(s).
# Used to dynamically set PROFILE_TIMEZONE in active.conf based on the proxy
# country (PROFILE_CARRIER_COUNTRY) rather than hardcoding per device profile.
# For multi-timezone countries (US, CA, AU, RU, etc.) a random one is picked.
# Comprehensive: covers all 249 ISO 3166-1 alpha-2 codes so no proxy exit
# country silently falls through without a timezone.
_COUNTRY_TIMEZONES: dict[str, list[str]] = {
    # ── Americas ──
    "US": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"],
    "CA": ["America/Toronto", "America/Vancouver", "America/Edmonton"],
    "MX": ["America/Mexico_City"],
    "BR": ["America/Sao_Paulo", "America/Manaus", "America/Recife"],
    "AR": ["America/Argentina/Buenos_Aires"],
    "CL": ["America/Santiago"],
    "CO": ["America/Bogota"],
    "PE": ["America/Lima"],
    "VE": ["America/Caracas"],
    "EC": ["America/Guayaquil"],
    "BO": ["America/La_Paz"],
    "PY": ["America/Asuncion"],
    "UY": ["America/Montevideo"],
    "GY": ["America/Guyana"],
    "SR": ["America/Paramaribo"],
    "GF": ["America/Cayenne"],
    "FK": ["Atlantic/Stanley"],
    "PA": ["America/Panama"],
    "CR": ["America/Costa_Rica"],
    "NI": ["America/Managua"],
    "HN": ["America/Tegucigalpa"],
    "SV": ["America/El_Salvador"],
    "GT": ["America/Guatemala"],
    "BZ": ["America/Belize"],
    "CU": ["America/Havana"],
    "DO": ["America/Santo_Domingo"],
    "HT": ["America/Port-au-Prince"],
    "JM": ["America/Jamaica"],
    "TT": ["America/Port_of_Spain"],
    "BB": ["America/Barbados"],
    "PR": ["America/Puerto_Rico"],
    "VI": ["America/Virgin"],
    "GU": ["Pacific/Guam"],
    "AS": ["Pacific/Pago_Pago"],
    "MP": ["Pacific/Guam"],
    "AW": ["America/Aruba"],
    "CW": ["America/Curacao"],
    "BM": ["Atlantic/Bermuda"],
    "KY": ["America/Cayman"],
    "BS": ["America/Nassau"],
    "AG": ["America/Antigua"],
    "DM": ["America/Dominica"],
    "GD": ["America/Grenada"],
    "KN": ["America/St_Kitts"],
    "LC": ["America/St_Lucia"],
    "VC": ["America/St_Vincent"],
    "TC": ["America/Grand_Turk"],
    "VG": ["America/Tortola"],
    "MQ": ["America/Martinique"],
    "GP": ["America/Guadeloupe"],
    "BQ": ["America/Kralendijk"],
    "SX": ["America/Lower_Princes"],
    "MF": ["America/Marigot"],
    "BL": ["America/St_Barthelemy"],
    "PM": ["America/Miquelon"],
    # ── Europe ──
    "GB": ["Europe/London"],
    "DE": ["Europe/Berlin"],
    "FR": ["Europe/Paris"],
    "NL": ["Europe/Amsterdam"],
    "ES": ["Europe/Madrid"],
    "IT": ["Europe/Rome"],
    "PL": ["Europe/Warsaw"],
    "SE": ["Europe/Stockholm"],
    "NO": ["Europe/Oslo"],
    "DK": ["Europe/Copenhagen"],
    "FI": ["Europe/Helsinki"],
    "CH": ["Europe/Zurich"],
    "AT": ["Europe/Vienna"],
    "BE": ["Europe/Brussels"],
    "PT": ["Europe/Lisbon"],
    "IE": ["Europe/Dublin"],
    "TR": ["Europe/Istanbul"],
    "RU": ["Europe/Moscow", "Asia/Yekaterinburg", "Asia/Novosibirsk", "Asia/Vladivostok"],
    "RO": ["Europe/Bucharest"],
    "CZ": ["Europe/Prague"],
    "HU": ["Europe/Budapest"],
    "GR": ["Europe/Athens"],
    "UA": ["Europe/Kiev"],
    "BY": ["Europe/Minsk"],
    "BG": ["Europe/Sofia"],
    "RS": ["Europe/Belgrade"],
    "HR": ["Europe/Zagreb"],
    "SK": ["Europe/Bratislava"],
    "SI": ["Europe/Ljubljana"],
    "LT": ["Europe/Vilnius"],
    "LV": ["Europe/Riga"],
    "EE": ["Europe/Tallinn"],
    "IS": ["Atlantic/Reykjavik"],
    "AL": ["Europe/Tirane"],
    "MK": ["Europe/Skopje"],
    "BA": ["Europe/Sarajevo"],
    "ME": ["Europe/Podgorica"],
    "XK": ["Europe/Belgrade"],
    "MD": ["Europe/Chisinau"],
    "LU": ["Europe/Luxembourg"],
    "MT": ["Europe/Malta"],
    "CY": ["Asia/Nicosia"],
    "LI": ["Europe/Vaduz"],
    "MC": ["Europe/Monaco"],
    "SM": ["Europe/San_Marino"],
    "AD": ["Europe/Andorra"],
    "GI": ["Europe/Gibraltar"],
    "FO": ["Atlantic/Faroe"],
    "AX": ["Europe/Mariehamn"],
    "JE": ["Europe/Jersey"],
    "GG": ["Europe/Guernsey"],
    "IM": ["Europe/Isle_of_Man"],
    "VA": ["Europe/Vatican"],
    # ── Africa ──
    "ZA": ["Africa/Johannesburg"],
    "EG": ["Africa/Cairo"],
    "NG": ["Africa/Lagos"],
    "KE": ["Africa/Nairobi"],
    "GH": ["Africa/Accra"],
    "TZ": ["Africa/Dar_es_Salaam"],
    "ET": ["Africa/Addis_Ababa"],
    "MA": ["Africa/Casablanca"],
    "DZ": ["Africa/Algiers"],
    "TN": ["Africa/Tunis"],
    "LY": ["Africa/Tripoli"],
    "SN": ["Africa/Dakar"],
    "CI": ["Africa/Abidjan"],
    "CM": ["Africa/Douala"],
    "AO": ["Africa/Luanda"],
    "MZ": ["Africa/Maputo"],
    "ZW": ["Africa/Harare"],
    "UG": ["Africa/Kampala"],
    "SD": ["Africa/Khartoum"],
    "SS": ["Africa/Juba"],
    "CD": ["Africa/Kinshasa", "Africa/Lubumbashi"],
    "CG": ["Africa/Brazzaville"],
    "GA": ["Africa/Libreville"],
    "RW": ["Africa/Kigali"],
    "BI": ["Africa/Bujumbura"],
    "MW": ["Africa/Blantyre"],
    "ZM": ["Africa/Lusaka"],
    "BW": ["Africa/Gaborone"],
    "NA": ["Africa/Windhoek"],
    "MG": ["Indian/Antananarivo"],
    "MU": ["Indian/Mauritius"],
    "RE": ["Indian/Reunion"],
    "SC": ["Indian/Mahe"],
    "DJ": ["Africa/Djibouti"],
    "ER": ["Africa/Asmara"],
    "SO": ["Africa/Mogadishu"],
    "ML": ["Africa/Bamako"],
    "BF": ["Africa/Ouagadougou"],
    "NE": ["Africa/Niamey"],
    "TD": ["Africa/Ndjamena"],
    "MR": ["Africa/Nouakchott"],
    "GM": ["Africa/Banjul"],
    "GW": ["Africa/Bissau"],
    "GN": ["Africa/Conakry"],
    "SL": ["Africa/Freetown"],
    "LR": ["Africa/Monrovia"],
    "TG": ["Africa/Lome"],
    "BJ": ["Africa/Porto-Novo"],
    "CV": ["Atlantic/Cape_Verde"],
    "ST": ["Africa/Sao_Tome"],
    "GQ": ["Africa/Malabo"],
    "CF": ["Africa/Bangui"],
    "KM": ["Indian/Comoro"],
    "YT": ["Indian/Mayotte"],
    "LS": ["Africa/Maseru"],
    "SZ": ["Africa/Mbabane"],
    # ── Asia ──
    "IN": ["Asia/Kolkata"],
    "JP": ["Asia/Tokyo"],
    "KR": ["Asia/Seoul"],
    "SG": ["Asia/Singapore"],
    "TH": ["Asia/Bangkok"],
    "PH": ["Asia/Manila"],
    "ID": ["Asia/Jakarta", "Asia/Makassar"],
    "MY": ["Asia/Kuala_Lumpur"],
    "VN": ["Asia/Ho_Chi_Minh"],
    "IL": ["Asia/Jerusalem"],
    "SA": ["Asia/Riyadh"],
    "AE": ["Asia/Dubai"],
    "PK": ["Asia/Karachi"],
    "BD": ["Asia/Dhaka"],
    "TW": ["Asia/Taipei"],
    "HK": ["Asia/Hong_Kong"],
    "CN": ["Asia/Shanghai"],
    "MO": ["Asia/Macau"],
    "MN": ["Asia/Ulaanbaatar"],
    "KH": ["Asia/Phnom_Penh"],
    "LA": ["Asia/Vientiane"],
    "MM": ["Asia/Yangon"],
    "NP": ["Asia/Kathmandu"],
    "LK": ["Asia/Colombo"],
    "AF": ["Asia/Kabul"],
    "IR": ["Asia/Tehran"],
    "IQ": ["Asia/Baghdad"],
    "SY": ["Asia/Damascus"],
    "LB": ["Asia/Beirut"],
    "JO": ["Asia/Amman"],
    "KW": ["Asia/Kuwait"],
    "QA": ["Asia/Qatar"],
    "BH": ["Asia/Bahrain"],
    "OM": ["Asia/Muscat"],
    "YE": ["Asia/Aden"],
    "GE": ["Asia/Tbilisi"],
    "AM": ["Asia/Yerevan"],
    "AZ": ["Asia/Baku"],
    "KZ": ["Asia/Almaty", "Asia/Aqtau"],
    "UZ": ["Asia/Tashkent"],
    "TM": ["Asia/Ashgabat"],
    "KG": ["Asia/Bishkek"],
    "TJ": ["Asia/Dushanbe"],
    "BN": ["Asia/Brunei"],
    "TL": ["Asia/Dili"],
    "BT": ["Asia/Thimphu"],
    "MV": ["Indian/Maldives"],
    "KP": ["Asia/Pyongyang"],
    "PS": ["Asia/Gaza"],
    # ── Oceania ──
    "AU": ["Australia/Sydney", "Australia/Melbourne", "Australia/Perth"],
    "NZ": ["Pacific/Auckland"],
    "FJ": ["Pacific/Fiji"],
    "PG": ["Pacific/Port_Moresby"],
    "NC": ["Pacific/Noumea"],
    "PF": ["Pacific/Tahiti"],
    "WS": ["Pacific/Apia"],
    "TO": ["Pacific/Tongatapu"],
    "VU": ["Pacific/Efate"],
    "SB": ["Pacific/Guadalcanal"],
    "KI": ["Pacific/Tarawa"],
    "MH": ["Pacific/Majuro"],
    "FM": ["Pacific/Chuuk"],
    "PW": ["Pacific/Palau"],
    "NR": ["Pacific/Nauru"],
    "TV": ["Pacific/Funafuti"],
    "CK": ["Pacific/Rarotonga"],
    "NU": ["Pacific/Niue"],
    "TK": ["Pacific/Fakaofo"],
    "WF": ["Pacific/Wallis"],
    "NF": ["Pacific/Norfolk"],
    "CC": ["Indian/Cocos"],
    "CX": ["Indian/Christmas"],
    "HM": ["Indian/Maldives"],
}


def _detect_ip_country(adb_exe: str, serial: str) -> tuple[str, str] | None:
    """Detect the exit IP's country code and IANA timezone via ip-api.com.

    Queries the device's actual outbound IP (through whatever proxy/VPN is active)
    and returns (country_code, timezone) — e.g. ("US", "America/New_York") or
    ("JP", "Asia/Tokyo").

    Returns None if the lookup fails (no internet, API down, etc.).
    """
    # ip-api.com free tier: 45 req/min, no key needed, returns plain text lines.
    # Request countryCode + timezone in a single call.
    cp = adb_shell(adb_exe, serial,
                   "su -c 'wget -qO- --timeout=8 "
                   "\"http://ip-api.com/line/?fields=countryCode,timezone\" 2>/dev/null'",
                   timeout_sec=15)
    if cp.returncode != 0 or not cp.stdout.strip():
        return None

    lines = cp.stdout.strip().splitlines()
    if len(lines) < 2:
        return None

    country = lines[0].strip().upper()
    timezone = lines[1].strip()

    # Sanity: country should be 2 chars, timezone should contain '/'
    if len(country) != 2 or '/' not in timezone:
        return None

    return (country, timezone)


def _patch_geo_from_ip(adb_exe: str, serial: str) -> dict[str, str] | None:
    """Detect exit IP country and patch CARRIER_COUNTRY + TIMEZONE in active.conf.

    Real Android phones auto-set timezone from the network, and the SIM carrier
    matches the country you're in. This function makes the device identity
    consistent with wherever the proxy/VPN exits:

      1. Queries ip-api.com from inside the device to get country + timezone
      2. Overwrites PROFILE_CARRIER_COUNTRY in all active.conf copies
      3. Overwrites PROFILE_TIMEZONE in all active.conf copies
      4. Applies timezone live via resetprop

    Returns {"country": "XX", "timezone": "Tz/Zone"} on success, None on failure.
    """
    result = _detect_ip_country(adb_exe, serial)
    if not result:
        print("       [warn] IP geolocation failed — carrier country & timezone unchanged")
        return None

    country, tz = result

    # Validate timezone: prefer the API's direct answer, but fall back to our
    # lookup table if the API returns something unexpected (e.g. empty or "Unknown")
    if not tz or tz == "Unknown":
        tz_list = _COUNTRY_TIMEZONES.get(country)
        if tz_list:
            tz = random.choice(tz_list)
        else:
            tz = "Etc/UTC"

    # Patch PROFILE_CARRIER_COUNTRY and PROFILE_TIMEZONE in all active.conf copies.
    # Each file is patched separately to avoid quoting issues with su -c.
    conf_files = [ACTIVE_CONF, READABLE_ACTIVE_CONF, "/data/local/tmp/jorkspoofer_active.conf"]
    cc = country.lower()
    for conf in conf_files:
        # Carrier country
        adb_shell(adb_exe, serial,
                  f"su -c \"sed -i 's|^PROFILE_CARRIER_COUNTRY=.*|PROFILE_CARRIER_COUNTRY=\\\"{cc}\\\"|' {conf}\"",
                  timeout_sec=6)
        # Timezone
        adb_shell(adb_exe, serial,
                  f"su -c \"sed -i 's|^PROFILE_TIMEZONE=.*|PROFILE_TIMEZONE=\\\"{tz}\\\"|' {conf}\"",
                  timeout_sec=6)

    # Apply timezone live
    adb_shell(adb_exe, serial,
              f"su -c \"resetprop persist.sys.timezone {tz}\"",
              timeout_sec=6)

    # Update the native module's timezone cache so that on next boot,
    # service.sh can use this as a fallback if IP lookup fails before VPN connects.
    adb_shell(adb_exe, serial,
              f"su -c 'echo \"{tz}\" > /data/adb/modules/jorkspoofer-native/cache/last_timezone 2>/dev/null; true'",
              timeout_sec=6)

    return {"country": country, "timezone": tz}


def _patch_carrier_identifiers(adb_exe: str, serial: str) -> None:
    """Patch IMSI and ICCID in identifiers.conf to match PROFILE_CARRIER_COUNTRY.

    The on-device identifiers.sh generates US-only IMSI (310260*) and ICCID (8901260*).
    This function reads the active profile's carrier country and rewrites the IMSI MCC/MNC
    and ICCID country prefix to match, ensuring cross-layer consistency between:
      - TelephonyHooks (Java IMSI → carrier) ↔ CDP (GPS country) ↔ Timezone
    """
    # Read PROFILE_CARRIER_COUNTRY from active.conf
    cp = adb_shell(adb_exe, serial,
                   f"su -c \"grep PROFILE_CARRIER_COUNTRY {ACTIVE_CONF} 2>/dev/null"
                   f" || grep PROFILE_CARRIER_COUNTRY {READABLE_ACTIVE_CONF} 2>/dev/null\"",
                   timeout_sec=8)
    if cp.returncode != 0 or not cp.stdout.strip():
        return  # No carrier country set — leave US defaults

    country_line = cp.stdout.strip().split("=", 1)
    if len(country_line) < 2:
        return
    country = country_line[1].strip().strip('"').strip("'").upper()
    if not country or country not in _CARRIER_MCC_MNC:
        return  # Unknown country — leave as-is

    # Pick a random MCC/MNC for this country
    mcc_mnc = random.choice(_CARRIER_MCC_MNC[country])
    mcc = mcc_mnc[:3]  # First 3 digits = MCC

    # Build new IMSI: MCC+MNC + random 10 digits (pad to 15 total)
    msin_len = 15 - len(mcc_mnc)
    new_imsi_suffix = ''.join([str(random.randint(0, 9)) for _ in range(msin_len)])
    new_imsi = mcc_mnc + new_imsi_suffix

    # Build new ICCID: "89" + MCC[1:3] + random 14 digits + Luhn check (19 total)
    iccid_prefix = "89" + mcc
    iccid_body_len = 19 - len(iccid_prefix) - 1  # -1 for Luhn check digit
    iccid_body = ''.join([str(random.randint(0, 9)) for _ in range(iccid_body_len)])
    iccid_base = iccid_prefix + iccid_body
    # Simple Luhn check digit
    digits = [int(d) for d in iccid_base]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    luhn = (10 - (total % 10)) % 10
    new_iccid = iccid_base + str(luhn)

    # Patch identifiers.conf on device via sed
    # Also patch the phone number country code to match
    phone_prefix = {
        "US": "+1", "GB": "+44", "DE": "+49", "FR": "+33", "NL": "+31",
        "ES": "+34", "IT": "+39", "CA": "+1", "AU": "+61", "BR": "+55",
        "IN": "+91", "JP": "+81", "KR": "+82", "SG": "+65", "MX": "+52",
        "PL": "+48", "SE": "+46", "NO": "+47", "DK": "+45", "FI": "+358",
        "CH": "+41", "AT": "+43", "BE": "+32", "PT": "+351", "IE": "+353",
        "NZ": "+64", "ZA": "+27", "AE": "+971", "TR": "+90", "RU": "+7",
        "TH": "+66", "PH": "+63", "ID": "+62", "MY": "+60", "VN": "+84",
        "CL": "+56", "CO": "+57", "AR": "+54", "EG": "+20", "NG": "+234",
        "KE": "+254", "IL": "+972", "SA": "+966", "PK": "+92", "BD": "+880",
        "TW": "+886", "HK": "+852", "RO": "+40", "CZ": "+420", "HU": "+36",
        "GR": "+30",
    }.get(country, "+1")
    phone_local = ''.join([str(random.randint(0 if i > 0 else 2, 9)) for i in range(10)])
    new_phone = phone_prefix + phone_local

    patch_cmd = (
        f"sed -i "
        f"-e 's/^PROFILE_IMSI=.*/PROFILE_IMSI=\"{new_imsi}\"/' "
        f"-e 's/^PROFILE_ICCID=.*/PROFILE_ICCID=\"{new_iccid}\"/' "
        f"-e 's/^PROFILE_PHONE_NUMBER=.*/PROFILE_PHONE_NUMBER=\"{new_phone}\"/' "
        f"/data/adb/jorkspoofer/identifiers.conf"
    )
    adb_shell(adb_exe, serial, f"su -c \"{patch_cmd}\"", timeout_sec=8)


def list_profiles(adb_exe: str, serial: str) -> list[str]:
    """List available .conf profile names on device (excluding active.conf)."""
    cmd = f"su -c \"ls {PROFILES_DIR}/*.conf 2>/dev/null\""
    cp = adb_shell(adb_exe, serial, cmd, timeout_sec=6)
    if cp.returncode != 0:
        return []
    profiles = []
    for line in cp.stdout.strip().splitlines():
        basename = line.strip().rsplit("/", 1)[-1]
        if basename and basename != "active.conf":
            profiles.append(basename)
    return profiles


def get_device_sdk(adb_exe: str, serial: str) -> str:
    """Get the device's actual SDK version via getprop."""
    cp = adb_shell(adb_exe, serial, "getprop ro.build.version.sdk", timeout_sec=6)
    if cp.returncode == 0:
        return cp.stdout.strip()
    return ""


def list_profiles_for_sdk(adb_exe: str, serial: str, device_sdk: str) -> list[str]:
    """List profiles that match the device's SDK version."""
    cmd = (
        f"su -c \"grep -l 'PROFILE_BUILD_VERSION_SDK=\\\"{device_sdk}\\\"' "
        f"{PROFILES_DIR}/*.conf 2>/dev/null\""
    )
    cp = adb_shell(adb_exe, serial, cmd, timeout_sec=10)
    matching = []
    if cp.returncode == 0:
        for line in cp.stdout.strip().splitlines():
            basename = line.strip().rsplit("/", 1)[-1]
            if basename and basename != "active.conf":
                matching.append(basename)
    return matching


def _regenerate_userscript(adb_exe: str, serial: str) -> None:
    """Read active profile from device and push an updated fingerprint userscript.

    Called after a profile switch to keep the browser-level fingerprint
    spoofing in sync with the native/system-level spoofing.  Non-fatal
    if it fails — the native layer still works.
    """
    try:
        import generate_userscript as gs
    except ImportError:
        # Running from a location where generate_userscript.py isn't importable
        script_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, script_dir)
        import generate_userscript as gs

    profile_data = gs.read_active_profile_from_device(adb_exe, serial)
    chrome_ver = gs.detect_chrome_version(adb_exe, serial)
    js_content = gs.generate_userscript(profile_data, chrome_ver)

    profile_name = profile_data.get("PROFILE_NAME", "Unknown")
    pushed = gs.push_userscript_to_device(js_content, adb_exe, serial)
    if pushed:
        print(f"       Userscript updated: {profile_name} (Chrome {chrome_ver})")
    else:
        print(f"       WARNING: Failed to push userscript to device")


def randomize_profile(adb_exe: str, serial: str) -> str:
    """Pick a random profile matching the device SDK and set it as active."""
    device_sdk = get_device_sdk(adb_exe, serial)
    profiles = []
    if device_sdk:
        profiles = list_profiles_for_sdk(adb_exe, serial, device_sdk)
        if profiles:
            print(f"       Found {len(profiles)} profiles matching SDK {device_sdk}")
        else:
            print(f"       WARNING: No profiles match SDK {device_sdk}, using all profiles")

    if not profiles:
        profiles = list_profiles(adb_exe, serial)

    if not profiles:
        raise RuntimeError("No profiles found in " + PROFILES_DIR)

    chosen = random.choice(profiles)
    src = f"{PROFILES_DIR}/{chosen}"
    cmd = (
        f"cp {src} {ACTIVE_CONF}; "
        f"cp {src} {READABLE_ACTIVE_CONF}; "
        f"chmod 0644 {READABLE_ACTIVE_CONF}; "
        f"cp {src} /data/local/tmp/jorkspoofer_active.conf; "
        f"chmod 0644 /data/local/tmp/jorkspoofer_active.conf"
    )
    cp = adb_shell(adb_exe, serial, f"su -c \"{cmd}\"", timeout_sec=10)
    if cp.returncode != 0:
        raise RuntimeError(f"Failed to set profile: {(cp.stdout + cp.stderr).strip()}")

    # ── CRITICAL: Call the on-device apply script to sync ALL layers ──
    # Without this, only the config files update — resetprop values,
    # hostname, timezone, /proc rebinds, and native_status stay stale.
    profile_name = chosen.replace(".conf", "")
    apply_cp = adb_shell(
        adb_exe, serial,
        f'su -c "sh /data/adb/jorkspoofer-switch.sh apply {profile_name}"',
        timeout_sec=30,
    )
    if apply_cp.returncode != 0:
        # Non-fatal: profile files are in place, props will apply on next reboot
        print(f"       WARNING: jorkspoofer-switch.sh apply returned "
              f"{apply_cp.returncode}: {(apply_cp.stdout + apply_cp.stderr).strip()}")
    else:
        output = apply_cp.stdout.strip()
        if output:
            print(f"       Profile applied live: {output[:120]}")

    # ── Generate & push browser fingerprint userscript for this profile ──
    try:
        _regenerate_userscript(adb_exe, serial)
    except Exception as e:
        print(f"       WARNING: userscript generation failed: {e}")

    return chosen


def read_identifiers(adb_exe: str, serial: str) -> dict:
    """Read current identifiers.conf and active profile from a running instance.

    Returns a dict with identifier key/value pairs plus ACTIVE_PROFILE name.
    """
    result = {}

    # Read identifiers.conf
    cp = adb_shell(adb_exe, serial,
                   "su -c \"cat /data/adb/jorkspoofer/identifiers.conf 2>/dev/null\"",
                   timeout_sec=10)
    if cp.returncode == 0 and cp.stdout.strip():
        for line in cp.stdout.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            eq = line.find("=")
            if eq > 0:
                key = line[:eq].strip()
                val = line[eq + 1:].strip().strip('"')
                result[key] = val

    # Read active profile name
    cp2 = adb_shell(adb_exe, serial,
                    "su -c \"cat /data/adb/jorkspoofer/active.conf 2>/dev/null\"",
                    timeout_sec=10)
    if cp2.returncode == 0 and cp2.stdout.strip():
        for line in cp2.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("PROFILE_NAME="):
                result["ACTIVE_PROFILE"] = line.split("=", 1)[1].strip().strip('"')
                break

    return result


def reset_identifiers(adb_exe: str, serial: str) -> None:
    """Delete and regenerate identifiers, then align everything to the exit IP.

    Flow:
      1. Regenerate random identifiers (IMEI, serial, Android ID, MAC, etc.)
      2. Detect exit IP country + timezone via ip-api.com
      3. Overwrite PROFILE_CARRIER_COUNTRY + PROFILE_TIMEZONE in active.conf
      4. Patch IMSI MCC/MNC + ICCID + phone prefix to match the detected country
      5. Apply timezone live via resetprop

    This ensures carrier, timezone, IMSI, and GPS all point to the same country
    as the proxy/VPN exit IP — exactly what a real phone would show.
    """
    cmd = (
        "rm -f /data/adb/jorkspoofer/identifiers.conf; "
        ". /data/adb/modules/jorkspoofer/config/settings.conf 2>/dev/null; "
        ". /data/adb/modules/jorkspoofer/lib/identifiers.sh 2>/dev/null; "
        "ensure_identifiers 2>/dev/null; "
        "ls -la /data/adb/jorkspoofer/identifiers.conf 2>/dev/null || true"
    )
    cp = adb_shell(adb_exe, serial, f"su -c \"{cmd}\"", timeout_sec=15)
    if cp.returncode != 0:
        raise RuntimeError((cp.stdout + "\n" + cp.stderr).strip())

    # ── Step 1: Detect exit IP country and patch carrier country + timezone ──
    geo = _patch_geo_from_ip(adb_exe, serial)
    if geo:
        print(f"       IP country: {geo['country']}  →  timezone: {geo['timezone']}")
    else:
        print("       [warn] IP geo failed — using profile's carrier country as fallback")

    # ── Step 2: Patch IMSI MCC/MNC to match the (now-correct) carrier country ──
    _patch_carrier_identifiers(adb_exe, serial)


def clear_dalvik_cache(adb_exe: str, serial: str) -> None:
    """Wipe dalvik-cache so ART recompiles apps on next boot with fresh identity."""
    cmd = "rm -rf /data/dalvik-cache/*"
    cp = adb_shell(adb_exe, serial, f"su -c \"{cmd}\"", timeout_sec=15)
    if cp.returncode != 0:
        raise RuntimeError(f"dalvik-cache clear failed: {(cp.stdout + cp.stderr).strip()}")


def sync_settings_db(adb_exe: str, serial: str) -> None:
    """Sync Settings database with profile and identifiers.

    Updates Settings.Secure.android_id, device_name, bluetooth_name, and
    wifi_p2p_device_name so they match the active profile and identifiers.
    These values live in the Settings database, not in build props.
    """
    # Read PROFILE_ANDROID_ID from identifiers.conf
    cp = adb_shell(adb_exe, serial,
                   "su -c \"grep PROFILE_ANDROID_ID /data/adb/jorkspoofer/identifiers.conf\"",
                   timeout_sec=6)
    if cp.returncode == 0 and "=" in cp.stdout:
        aid = cp.stdout.strip().split("=", 1)[1].strip().strip('"')
        if aid:
            adb_shell(adb_exe, serial,
                      f"su -c \"settings put secure android_id {aid}\"",
                      timeout_sec=6)

    # Read PROFILE_MODEL from active.conf for device name fields
    cp2 = adb_shell(adb_exe, serial,
                    f"su -c \"grep PROFILE_MODEL {ACTIVE_CONF}\"",
                    timeout_sec=6)
    if cp2.returncode == 0 and "=" in cp2.stdout:
        model = cp2.stdout.strip().split("=", 1)[1].strip().strip('"')
        if model:
            for setting in [
                f"settings put global device_name '{model}'",
                f"settings put secure bluetooth_name '{model}'",
                f"settings put global wifi_p2p_device_name '{model}'",
            ]:
                adb_shell(adb_exe, serial, f"su -c \"{setting}\"", timeout_sec=6)


def reboot_instance(adb_exe: str, serial: str) -> None:
    cp = adb_shell(adb_exe, serial, "su -c reboot", timeout_sec=6)
    if cp.returncode != 0:
        _ = adb_shell(adb_exe, serial, "reboot", timeout_sec=6)


# Packages whose processes should be force-stopped after a hot profile swap
# so they re-launch with the new Xposed-hooked identity values.
# Note: Chrome is handled by CDP (browser layer); no need to force-stop it here.
TARGET_APP_PACKAGES = [
    "com.jagex.oldscape.android",     # Old School RuneScape
    "com.jagex.oldschool.android",    # OSRS (alternate package name seen in some regions)
]


def restart_target_apps(adb_exe: str, serial: str,
                        extra_packages: list[str] | None = None) -> list[str]:
    """Force-stop target app packages so they restart fresh with new hook values.

    After a profile hot-swap (skip_reboot=True), running apps still hold old
    Build.*, GL strings, identifiers, etc. cached in Java static fields.
    Force-stopping them means the next launch will fork from Zygote, triggering
    handleLoadPackage → ConfigManager.init() → fresh config read.

    Returns list of packages that were successfully stopped.
    """
    packages = list(TARGET_APP_PACKAGES)
    if extra_packages:
        packages.extend(extra_packages)

    stopped = []
    for pkg in packages:
        try:
            cp = adb_shell(adb_exe, serial, f"su -c 'am force-stop {pkg}'", timeout_sec=5)
            if cp.returncode == 0:
                stopped.append(pkg)
        except Exception:
            pass  # Non-fatal: package might not be installed

    return stopped


def select_instances_interactive(candidates: list[InstanceInfo], running_flags: dict[str, bool]) -> list[InstanceInfo]:
    print("\nInstances:")
    for idx, inst in enumerate(candidates, start=1):
        serial = f"{DEFAULT_HOST}:{inst.adb_port}"
        disp = inst.display_name or inst.name
        status = "RUNNING" if running_flags.get(serial) else "offline"
        print(f"  {idx:2d}. {inst.name:20s} port={inst.adb_port:<5d} {status:7s}  {disp}")

    raw = input("\nSelect instances by number (e.g. 1,3-5) or 'a' for all running: ").strip().lower()
    if raw == "a":
        return [i for i in candidates if running_flags.get(f"{DEFAULT_HOST}:{i.adb_port}")]

    chosen: set[int] = set()
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            for n in range(min(lo, hi), max(lo, hi) + 1):
                chosen.add(n)
        else:
            try:
                chosen.add(int(part))
            except ValueError:
                continue

    out: list[InstanceInfo] = []
    for n in sorted(chosen):
        if 1 <= n <= len(candidates):
            out.append(candidates[n - 1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset per-instance device identifiers via ADB (macOS) — Luke's Mirage.")
    ap.add_argument("--bluestacks-dir", default=DEFAULT_BLUESTACKS_DIR, help="BlueStacks Air app contents (for hd-adb).")
    ap.add_argument("--conf", help="Path to bluestacks.conf (auto-detected if omitted).")
    ap.add_argument("--adb", help="Path to adb executable (defaults to hd-adb if present).")
    ap.add_argument("--host", default=DEFAULT_HOST, help="ADB host (default 127.0.0.1).")
    ap.add_argument("--list", action="store_true", help="List instances and exit.")
    ap.add_argument("--running", action="store_true", help="Only target instances that are booted/running.")
    ap.add_argument("--select", nargs="*", help="Instance names to target (from bluestacks.conf).")
    ap.add_argument("--ports", nargs="*", type=int, help="ADB ports to target.")
    ap.add_argument("--no-reboot", action="store_true", help="Do not reboot after reset.")
    ap.add_argument("--randomize-profile", action="store_true", help="Also randomize the device profile.")
    ap.add_argument("--non-interactive", action="store_true", help="Do not prompt; fail if no targets specified.")
    args = ap.parse_args()

    if sys.platform != "darwin":
        print("ERROR: This script is for macOS only (BlueStacks Air).")
        print("For Windows, use the version in the parent directory.")
        return 2

    adb_exe = args.adb or find_adb_exe(args.bluestacks_dir)

    if args.conf:
        conf_path = Path(args.conf)
    else:
        engine_dir = detect_engine_dir()
        if engine_dir is None:
            print("ERROR: Could not auto-detect BlueStacks Air data directory. Pass --conf.")
            return 2
        conf_path = engine_dir.parent / "bluestacks.conf"

    if not conf_path.is_file():
        print(f"ERROR: bluestacks.conf not found at {conf_path}")
        return 2

    instances = discover_instances_from_conf(conf_path)
    if not instances:
        print(f"ERROR: No instances with adb_port discovered in {conf_path}")
        return 2

    # Probe running status for all discovered instances
    running: dict[str, bool] = {}
    root_ok: dict[str, bool] = {}
    for inst in instances:
        serial = f"{args.host}:{inst.adb_port}"
        if not adb_connect(adb_exe, serial):
            running[serial] = False
            root_ok[serial] = False
            continue
        running[serial] = probe_boot_completed(adb_exe, serial)
        root_ok[serial] = probe_root(adb_exe, serial) if running[serial] else False

    if args.list:
        print(f"Conf: {conf_path}")
        print(f"ADB:  {adb_exe}")
        for inst in instances:
            serial = f"{args.host}:{inst.adb_port}"
            disp = inst.display_name or inst.name
            st = "RUNNING" if running.get(serial) else "offline"
            rt = "root" if root_ok.get(serial) else "no-root"
            print(f"- {inst.name:20s} port={inst.adb_port:<5d} {st:7s} {rt:7s}  {disp}")
        return 0

    # Build selection
    selected: list[InstanceInfo] = []
    if args.ports:
        portset = set(args.ports)
        selected = [i for i in instances if i.adb_port in portset]
    elif args.select:
        names = set(args.select)
        selected = [i for i in instances if i.name in names]
    elif not args.non_interactive:
        candidates = [i for i in instances if (running.get(f"{args.host}:{i.adb_port}") if args.running else True)]
        selected = select_instances_interactive(candidates, running)

    if not selected:
        print("ERROR: No instances selected.")
        return 2

    # Enforce running filter
    if args.running:
        selected = [i for i in selected if running.get(f"{args.host}:{i.adb_port}")]
        if not selected:
            print("ERROR: None of the selected instances are RUNNING.")
            return 2

    # Execute
    for inst in selected:
        serial = f"{args.host}:{inst.adb_port}"
        disp = inst.display_name or inst.name

        if not running.get(serial):
            print(f"[skip] {inst.name} ({disp}) at {serial}: not running/booted")
            continue
        if not root_ok.get(serial):
            print(f"[skip] {inst.name} ({disp}) at {serial}: no root via 'su -c'")
            continue

        if args.randomize_profile:
            print(f"[do]   {inst.name} ({disp}) at {serial}: randomize profile")
            try:
                chosen = randomize_profile(adb_exe, serial)
                print(f"[ok]   {inst.name} at {serial}: profile -> {chosen}")
            except Exception as e:
                print(f"[fail] {inst.name} at {serial}: profile randomization: {e}")

        print(f"[do]   {inst.name} ({disp}) at {serial}: reset identifiers")
        try:
            reset_identifiers(adb_exe, serial)
        except Exception as e:
            print(f"[fail] {inst.name} at {serial}: {e}")
            continue

        print(f"[do]   {inst.name} ({disp}) at {serial}: sync settings database")
        try:
            sync_settings_db(adb_exe, serial)
            print(f"[ok]   {inst.name} at {serial}: settings database synced")
        except Exception as e:
            print(f"[fail] {inst.name} at {serial}: settings sync: {e}")

        print(f"[do]   {inst.name} ({disp}) at {serial}: clear dalvik cache")
        try:
            clear_dalvik_cache(adb_exe, serial)
            print(f"[ok]   {inst.name} at {serial}: dalvik cache cleared")
        except Exception as e:
            print(f"[fail] {inst.name} at {serial}: dalvik cache: {e}")

        if args.no_reboot:
            print(f"[ok]   {inst.name} at {serial}: done (no reboot)")
        else:
            print(f"[do]   {inst.name} at {serial}: reboot")
            reboot_instance(adb_exe, serial)
            print(f"[ok]   {inst.name} at {serial}: done (reboot issued)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

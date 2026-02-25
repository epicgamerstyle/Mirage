#!/usr/bin/env python3
"""
Luke's Mirage — Chrome DevTools Protocol (CDP) Browser Fingerprint Injector.

Connects to Chrome / WebView on running BlueStacks Air instances via ADB port
forwarding and injects a comprehensive fingerprint-spoofing JS payload using
Page.addScriptToEvaluateOnNewDocument.  The payload overrides navigator.*,
screen.*, WebGL, Canvas, AudioContext and HTTP Client-Hints so that websites
see the same spoofed device identity that the Android-side Xposed hooks present.
"""

import hashlib
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

try:
    import websocket  # websocket-client package
except ImportError:
    websocket = None

# ─── Constants ──────────────────────────────────────────────────────────────

# On-device profile paths (same as randomize_instances.py)
ACTIVE_CONF_PATH = "/data/adb/modules/jorkspoofer/profiles/active.conf"
READABLE_CONF_PATH = "/data/adb/jorkspoofer/active.conf"
READABLE_CONF_PATH_TMP = "/data/local/tmp/jorkspoofer_active.conf"

# Chrome DevTools socket name on Android
DEVTOOLS_SOCKET = "chrome_devtools_remote"

# Polling interval (seconds) when waiting for Chrome to open
CHROME_POLL_INTERVAL = 3.0

# Tab monitor interval (seconds) for detecting new tabs
TAB_MONITOR_INTERVAL = 2.0

# Base port for local ADB forwarding (each instance gets base + offset)
CDP_LOCAL_PORT_BASE = 19200

# ─── Carrier ↔ Country Database ──────────────────────────────────────────────

# Mapping: ISO country code → list of (carrier_name, MCC+MNC, timezone)
# Each entry represents a realistic mobile carrier for that country.
# The randomizer picks one randomly when correlating with VPN/proxy country.
# MCC/MNC codes sourced from mcc-mnc.com / ITU data.
CARRIER_DB: dict[str, list[dict[str, str]]] = {
    "US": [
        {"name": "T-Mobile",   "mcc_mnc": "310260", "tz": "America/New_York"},
        {"name": "T-Mobile",   "mcc_mnc": "310260", "tz": "America/Chicago"},
        {"name": "T-Mobile",   "mcc_mnc": "310260", "tz": "America/Denver"},
        {"name": "T-Mobile",   "mcc_mnc": "310260", "tz": "America/Los_Angeles"},
        {"name": "AT&T",       "mcc_mnc": "310410", "tz": "America/New_York"},
        {"name": "AT&T",       "mcc_mnc": "310410", "tz": "America/Chicago"},
        {"name": "Verizon",    "mcc_mnc": "311480", "tz": "America/New_York"},
        {"name": "Verizon",    "mcc_mnc": "311480", "tz": "America/Chicago"},
    ],
    "GB": [
        {"name": "EE",         "mcc_mnc": "23430",  "tz": "Europe/London"},
        {"name": "Vodafone UK","mcc_mnc": "23415",  "tz": "Europe/London"},
        {"name": "Three UK",   "mcc_mnc": "23420",  "tz": "Europe/London"},
        {"name": "O2 - UK",    "mcc_mnc": "23410",  "tz": "Europe/London"},
    ],
    "DE": [
        {"name": "Telekom.de", "mcc_mnc": "26201",  "tz": "Europe/Berlin"},
        {"name": "Vodafone.de","mcc_mnc": "26202",  "tz": "Europe/Berlin"},
        {"name": "o2 - de",    "mcc_mnc": "26207",  "tz": "Europe/Berlin"},
    ],
    "FR": [
        {"name": "Orange F",   "mcc_mnc": "20801",  "tz": "Europe/Paris"},
        {"name": "SFR",        "mcc_mnc": "20810",  "tz": "Europe/Paris"},
        {"name": "Bouygues",   "mcc_mnc": "20820",  "tz": "Europe/Paris"},
        {"name": "Free Mobile","mcc_mnc": "20815",  "tz": "Europe/Paris"},
    ],
    "NL": [
        {"name": "KPN",        "mcc_mnc": "20408",  "tz": "Europe/Amsterdam"},
        {"name": "T-Mobile NL","mcc_mnc": "20416",  "tz": "Europe/Amsterdam"},
        {"name": "Vodafone NL","mcc_mnc": "20404",  "tz": "Europe/Amsterdam"},
    ],
    "ES": [
        {"name": "Movistar",   "mcc_mnc": "21407",  "tz": "Europe/Madrid"},
        {"name": "Vodafone ES","mcc_mnc": "21401",  "tz": "Europe/Madrid"},
        {"name": "Orange ES",  "mcc_mnc": "21403",  "tz": "Europe/Madrid"},
    ],
    "IT": [
        {"name": "TIM",        "mcc_mnc": "22201",  "tz": "Europe/Rome"},
        {"name": "Vodafone IT","mcc_mnc": "22210",  "tz": "Europe/Rome"},
        {"name": "WIND TRE",   "mcc_mnc": "22288",  "tz": "Europe/Rome"},
    ],
    "CA": [
        {"name": "Rogers",     "mcc_mnc": "302720", "tz": "America/Toronto"},
        {"name": "Bell",       "mcc_mnc": "302610", "tz": "America/Toronto"},
        {"name": "Telus",      "mcc_mnc": "302220", "tz": "America/Vancouver"},
    ],
    "AU": [
        {"name": "Telstra",    "mcc_mnc": "50501",  "tz": "Australia/Sydney"},
        {"name": "Optus",      "mcc_mnc": "50502",  "tz": "Australia/Sydney"},
        {"name": "Vodafone AU","mcc_mnc": "50503",  "tz": "Australia/Melbourne"},
    ],
    "BR": [
        {"name": "Claro BR",   "mcc_mnc": "72405",  "tz": "America/Sao_Paulo"},
        {"name": "Vivo",       "mcc_mnc": "72406",  "tz": "America/Sao_Paulo"},
        {"name": "TIM BR",     "mcc_mnc": "72404",  "tz": "America/Sao_Paulo"},
    ],
    "IN": [
        {"name": "Jio",        "mcc_mnc": "40586",  "tz": "Asia/Kolkata"},
        {"name": "Airtel",     "mcc_mnc": "40410",  "tz": "Asia/Kolkata"},
        {"name": "Vi",         "mcc_mnc": "40411",  "tz": "Asia/Kolkata"},
    ],
    "JP": [
        {"name": "NTT DOCOMO", "mcc_mnc": "44010",  "tz": "Asia/Tokyo"},
        {"name": "SoftBank",   "mcc_mnc": "44020",  "tz": "Asia/Tokyo"},
        {"name": "au (KDDI)",  "mcc_mnc": "44050",  "tz": "Asia/Tokyo"},
    ],
    "KR": [
        {"name": "SKT",        "mcc_mnc": "45005",  "tz": "Asia/Seoul"},
        {"name": "KT",         "mcc_mnc": "45008",  "tz": "Asia/Seoul"},
        {"name": "LG U+",      "mcc_mnc": "45006",  "tz": "Asia/Seoul"},
    ],
    "SG": [
        {"name": "Singtel",    "mcc_mnc": "52501",  "tz": "Asia/Singapore"},
        {"name": "StarHub",    "mcc_mnc": "52505",  "tz": "Asia/Singapore"},
        {"name": "M1",         "mcc_mnc": "52503",  "tz": "Asia/Singapore"},
    ],
    "MX": [
        {"name": "Telcel",     "mcc_mnc": "334020", "tz": "America/Mexico_City"},
        {"name": "AT&T MX",    "mcc_mnc": "334050", "tz": "America/Mexico_City"},
    ],
    "PL": [
        {"name": "Orange PL",  "mcc_mnc": "26003",  "tz": "Europe/Warsaw"},
        {"name": "Play",       "mcc_mnc": "26006",  "tz": "Europe/Warsaw"},
        {"name": "T-Mobile PL","mcc_mnc": "26002",  "tz": "Europe/Warsaw"},
    ],
    "SE": [
        {"name": "Telia SE",   "mcc_mnc": "24001",  "tz": "Europe/Stockholm"},
        {"name": "Tele2 SE",   "mcc_mnc": "24007",  "tz": "Europe/Stockholm"},
    ],
    "NO": [
        {"name": "Telenor NO", "mcc_mnc": "24201",  "tz": "Europe/Oslo"},
        {"name": "Telia NO",   "mcc_mnc": "24202",  "tz": "Europe/Oslo"},
    ],
    "DK": [
        {"name": "TDC",        "mcc_mnc": "23801",  "tz": "Europe/Copenhagen"},
        {"name": "Telia DK",   "mcc_mnc": "23802",  "tz": "Europe/Copenhagen"},
    ],
    "FI": [
        {"name": "Elisa",      "mcc_mnc": "24405",  "tz": "Europe/Helsinki"},
        {"name": "Telia FI",   "mcc_mnc": "24491",  "tz": "Europe/Helsinki"},
    ],
    "CH": [
        {"name": "Swisscom",   "mcc_mnc": "22801",  "tz": "Europe/Zurich"},
        {"name": "Sunrise",    "mcc_mnc": "22802",  "tz": "Europe/Zurich"},
    ],
    "AT": [
        {"name": "A1 AT",      "mcc_mnc": "23201",  "tz": "Europe/Vienna"},
        {"name": "Magenta AT", "mcc_mnc": "23203",  "tz": "Europe/Vienna"},
    ],
    "BE": [
        {"name": "Proximus",   "mcc_mnc": "20601",  "tz": "Europe/Brussels"},
        {"name": "Orange BE",  "mcc_mnc": "20610",  "tz": "Europe/Brussels"},
    ],
    "PT": [
        {"name": "MEO",        "mcc_mnc": "26806",  "tz": "Europe/Lisbon"},
        {"name": "NOS",        "mcc_mnc": "26803",  "tz": "Europe/Lisbon"},
    ],
    "IE": [
        {"name": "Three IE",   "mcc_mnc": "27205",  "tz": "Europe/Dublin"},
        {"name": "Vodafone IE","mcc_mnc": "27201",  "tz": "Europe/Dublin"},
    ],
    "NZ": [
        {"name": "Spark NZ",   "mcc_mnc": "53005",  "tz": "Pacific/Auckland"},
        {"name": "Vodafone NZ","mcc_mnc": "53001",  "tz": "Pacific/Auckland"},
    ],
    "ZA": [
        {"name": "Vodacom",    "mcc_mnc": "65501",  "tz": "Africa/Johannesburg"},
        {"name": "MTN SA",     "mcc_mnc": "65510",  "tz": "Africa/Johannesburg"},
    ],
    "AE": [
        {"name": "Etisalat",   "mcc_mnc": "42402",  "tz": "Asia/Dubai"},
        {"name": "du",         "mcc_mnc": "42403",  "tz": "Asia/Dubai"},
    ],
    "TR": [
        {"name": "Turkcell",   "mcc_mnc": "28601",  "tz": "Europe/Istanbul"},
        {"name": "Vodafone TR","mcc_mnc": "28602",  "tz": "Europe/Istanbul"},
    ],
    "RU": [
        {"name": "MTS",        "mcc_mnc": "25001",  "tz": "Europe/Moscow"},
        {"name": "MegaFon",    "mcc_mnc": "25002",  "tz": "Europe/Moscow"},
        {"name": "Beeline RU", "mcc_mnc": "25099",  "tz": "Europe/Moscow"},
    ],
    "TH": [
        {"name": "AIS",        "mcc_mnc": "52001",  "tz": "Asia/Bangkok"},
        {"name": "DTAC",       "mcc_mnc": "52005",  "tz": "Asia/Bangkok"},
    ],
    "PH": [
        {"name": "Globe",      "mcc_mnc": "51502",  "tz": "Asia/Manila"},
        {"name": "Smart",      "mcc_mnc": "51503",  "tz": "Asia/Manila"},
    ],
    "ID": [
        {"name": "Telkomsel",  "mcc_mnc": "51010",  "tz": "Asia/Jakarta"},
        {"name": "XL Axiata",  "mcc_mnc": "51011",  "tz": "Asia/Jakarta"},
    ],
    "MY": [
        {"name": "Maxis",      "mcc_mnc": "50212",  "tz": "Asia/Kuala_Lumpur"},
        {"name": "Celcom",     "mcc_mnc": "50213",  "tz": "Asia/Kuala_Lumpur"},
    ],
    "VN": [
        {"name": "Viettel",    "mcc_mnc": "45204",  "tz": "Asia/Ho_Chi_Minh"},
        {"name": "Mobifone",   "mcc_mnc": "45201",  "tz": "Asia/Ho_Chi_Minh"},
    ],
    "CL": [
        {"name": "Entel CL",   "mcc_mnc": "73001",  "tz": "America/Santiago"},
        {"name": "Movistar CL","mcc_mnc": "73002",  "tz": "America/Santiago"},
    ],
    "CO": [
        {"name": "Claro CO",   "mcc_mnc": "732101", "tz": "America/Bogota"},
        {"name": "Movistar CO","mcc_mnc": "732123", "tz": "America/Bogota"},
    ],
    "AR": [
        {"name": "Claro AR",   "mcc_mnc": "722310", "tz": "America/Argentina/Buenos_Aires"},
        {"name": "Personal",   "mcc_mnc": "722340", "tz": "America/Argentina/Buenos_Aires"},
    ],
    "EG": [
        {"name": "Vodafone EG","mcc_mnc": "60202",  "tz": "Africa/Cairo"},
        {"name": "Orange EG",  "mcc_mnc": "60201",  "tz": "Africa/Cairo"},
    ],
    "NG": [
        {"name": "MTN NG",     "mcc_mnc": "62130",  "tz": "Africa/Lagos"},
        {"name": "Airtel NG",  "mcc_mnc": "62120",  "tz": "Africa/Lagos"},
    ],
    "KE": [
        {"name": "Safaricom",  "mcc_mnc": "63902",  "tz": "Africa/Nairobi"},
        {"name": "Airtel KE",  "mcc_mnc": "63905",  "tz": "Africa/Nairobi"},
    ],
    "IL": [
        {"name": "Cellcom",    "mcc_mnc": "42502",  "tz": "Asia/Jerusalem"},
        {"name": "Partner",    "mcc_mnc": "42501",  "tz": "Asia/Jerusalem"},
    ],
    "SA": [
        {"name": "STC",        "mcc_mnc": "42001",  "tz": "Asia/Riyadh"},
        {"name": "Mobily",     "mcc_mnc": "42003",  "tz": "Asia/Riyadh"},
    ],
    "PK": [
        {"name": "Jazz",       "mcc_mnc": "41001",  "tz": "Asia/Karachi"},
        {"name": "Telenor PK", "mcc_mnc": "41006",  "tz": "Asia/Karachi"},
    ],
    "BD": [
        {"name": "Grameenphone", "mcc_mnc": "47001", "tz": "Asia/Dhaka"},
        {"name": "Robi",       "mcc_mnc": "47002",  "tz": "Asia/Dhaka"},
    ],
    "TW": [
        {"name": "Chunghwa",   "mcc_mnc": "46692",  "tz": "Asia/Taipei"},
        {"name": "Taiwan Mobile","mcc_mnc": "46697", "tz": "Asia/Taipei"},
    ],
    "HK": [
        {"name": "3 HK",       "mcc_mnc": "45403",  "tz": "Asia/Hong_Kong"},
        {"name": "CSL",        "mcc_mnc": "45400",  "tz": "Asia/Hong_Kong"},
    ],
    "RO": [
        {"name": "Vodafone RO","mcc_mnc": "22601",  "tz": "Europe/Bucharest"},
        {"name": "Orange RO",  "mcc_mnc": "22610",  "tz": "Europe/Bucharest"},
    ],
    "CZ": [
        {"name": "O2 CZ",      "mcc_mnc": "23002",  "tz": "Europe/Prague"},
        {"name": "T-Mobile CZ","mcc_mnc": "23001",  "tz": "Europe/Prague"},
    ],
    "HU": [
        {"name": "Magyar Telekom","mcc_mnc": "21630","tz": "Europe/Budapest"},
        {"name": "Vodafone HU","mcc_mnc": "21670",  "tz": "Europe/Budapest"},
    ],
    "GR": [
        {"name": "Cosmote",    "mcc_mnc": "20201",  "tz": "Europe/Athens"},
        {"name": "Vodafone GR","mcc_mnc": "20205",  "tz": "Europe/Athens"},
    ],
}


# ─── GPS Coordinates Database ────────────────────────────────────────────────

# Mapping: ISO country code → list of (city, latitude, longitude, accuracy_m)
# Each entry is a major city where a real phone user would plausibly be.
# Coordinates are city-center with slight urban area spread.
# Accuracy values mimic real Android fused-location (20-80m for cellular/Wi-Fi).
# The randomizer picks one city per identity, then adds deterministic jitter
# (±0.005° ≈ 500m) so every session for the same identity has a stable location.
GPS_COORDINATES: dict[str, list[dict]] = {
    "US": [
        {"city": "New York",       "lat": 40.7128,  "lon": -74.0060, "acc": 30},
        {"city": "Los Angeles",    "lat": 34.0522,  "lon": -118.2437,"acc": 35},
        {"city": "Chicago",        "lat": 41.8781,  "lon": -87.6298, "acc": 25},
        {"city": "Houston",        "lat": 29.7604,  "lon": -95.3698, "acc": 40},
        {"city": "Phoenix",        "lat": 33.4484,  "lon": -112.0740,"acc": 45},
        {"city": "Dallas",         "lat": 32.7767,  "lon": -96.7970, "acc": 30},
        {"city": "Atlanta",        "lat": 33.7490,  "lon": -84.3880, "acc": 25},
        {"city": "Miami",          "lat": 25.7617,  "lon": -80.1918, "acc": 35},
        {"city": "Seattle",        "lat": 47.6062,  "lon": -122.3321,"acc": 20},
        {"city": "Denver",         "lat": 39.7392,  "lon": -104.9903,"acc": 30},
    ],
    "GB": [
        {"city": "London",         "lat": 51.5074,  "lon": -0.1278,  "acc": 20},
        {"city": "Manchester",     "lat": 53.4808,  "lon": -2.2426,  "acc": 30},
        {"city": "Birmingham",     "lat": 52.4862,  "lon": -1.8904,  "acc": 35},
        {"city": "Leeds",          "lat": 53.8008,  "lon": -1.5491,  "acc": 40},
    ],
    "DE": [
        {"city": "Berlin",         "lat": 52.5200,  "lon": 13.4050,  "acc": 25},
        {"city": "Munich",         "lat": 48.1351,  "lon": 11.5820,  "acc": 30},
        {"city": "Hamburg",        "lat": 53.5511,  "lon": 9.9937,   "acc": 25},
        {"city": "Frankfurt",      "lat": 50.1109,  "lon": 8.6821,   "acc": 20},
    ],
    "FR": [
        {"city": "Paris",          "lat": 48.8566,  "lon": 2.3522,   "acc": 20},
        {"city": "Lyon",           "lat": 45.7640,  "lon": 4.8357,   "acc": 30},
        {"city": "Marseille",      "lat": 43.2965,  "lon": 5.3698,   "acc": 35},
        {"city": "Toulouse",       "lat": 43.6047,  "lon": 1.4442,   "acc": 30},
    ],
    "NL": [
        {"city": "Amsterdam",      "lat": 52.3676,  "lon": 4.9041,   "acc": 20},
        {"city": "Rotterdam",      "lat": 51.9244,  "lon": 4.4777,   "acc": 25},
        {"city": "Utrecht",        "lat": 52.0907,  "lon": 5.1214,   "acc": 30},
    ],
    "ES": [
        {"city": "Madrid",         "lat": 40.4168,  "lon": -3.7038,  "acc": 25},
        {"city": "Barcelona",      "lat": 41.3851,  "lon": 2.1734,   "acc": 20},
        {"city": "Valencia",       "lat": 39.4699,  "lon": -0.3763,  "acc": 35},
    ],
    "IT": [
        {"city": "Rome",           "lat": 41.9028,  "lon": 12.4964,  "acc": 25},
        {"city": "Milan",          "lat": 45.4642,  "lon": 9.1900,   "acc": 20},
        {"city": "Naples",         "lat": 40.8518,  "lon": 14.2681,  "acc": 35},
    ],
    "CA": [
        {"city": "Toronto",        "lat": 43.6532,  "lon": -79.3832, "acc": 25},
        {"city": "Vancouver",      "lat": 49.2827,  "lon": -123.1207,"acc": 20},
        {"city": "Montreal",       "lat": 45.5017,  "lon": -73.5673, "acc": 30},
    ],
    "AU": [
        {"city": "Sydney",         "lat": -33.8688, "lon": 151.2093, "acc": 25},
        {"city": "Melbourne",      "lat": -37.8136, "lon": 144.9631, "acc": 30},
        {"city": "Brisbane",       "lat": -27.4698, "lon": 153.0251, "acc": 35},
    ],
    "BR": [
        {"city": "São Paulo",      "lat": -23.5505, "lon": -46.6333, "acc": 40},
        {"city": "Rio de Janeiro",  "lat": -22.9068, "lon": -43.1729, "acc": 35},
        {"city": "Brasília",       "lat": -15.7975, "lon": -47.8919, "acc": 45},
    ],
    "IN": [
        {"city": "Mumbai",         "lat": 19.0760,  "lon": 72.8777,  "acc": 50},
        {"city": "Delhi",          "lat": 28.7041,  "lon": 77.1025,  "acc": 45},
        {"city": "Bangalore",      "lat": 12.9716,  "lon": 77.5946,  "acc": 40},
        {"city": "Hyderabad",      "lat": 17.3850,  "lon": 78.4867,  "acc": 45},
    ],
    "JP": [
        {"city": "Tokyo",          "lat": 35.6762,  "lon": 139.6503, "acc": 15},
        {"city": "Osaka",          "lat": 34.6937,  "lon": 135.5023, "acc": 20},
        {"city": "Yokohama",       "lat": 35.4437,  "lon": 139.6380, "acc": 20},
    ],
    "KR": [
        {"city": "Seoul",          "lat": 37.5665,  "lon": 126.9780, "acc": 20},
        {"city": "Busan",          "lat": 35.1796,  "lon": 129.0756, "acc": 25},
        {"city": "Incheon",        "lat": 37.4563,  "lon": 126.7052, "acc": 30},
    ],
    "SG": [
        {"city": "Singapore",      "lat": 1.3521,   "lon": 103.8198, "acc": 15},
    ],
    "MX": [
        {"city": "Mexico City",    "lat": 19.4326,  "lon": -99.1332, "acc": 40},
        {"city": "Guadalajara",    "lat": 20.6597,  "lon": -103.3496,"acc": 45},
    ],
    "PL": [
        {"city": "Warsaw",         "lat": 52.2297,  "lon": 21.0122,  "acc": 25},
        {"city": "Kraków",         "lat": 50.0647,  "lon": 19.9450,  "acc": 30},
    ],
    "SE": [
        {"city": "Stockholm",      "lat": 59.3293,  "lon": 18.0686,  "acc": 20},
        {"city": "Gothenburg",     "lat": 57.7089,  "lon": 11.9746,  "acc": 25},
    ],
    "NO": [
        {"city": "Oslo",           "lat": 59.9139,  "lon": 10.7522,  "acc": 20},
        {"city": "Bergen",         "lat": 60.3913,  "lon": 5.3221,   "acc": 30},
    ],
    "DK": [
        {"city": "Copenhagen",     "lat": 55.6761,  "lon": 12.5683,  "acc": 20},
        {"city": "Aarhus",         "lat": 56.1629,  "lon": 10.2039,  "acc": 30},
    ],
    "FI": [
        {"city": "Helsinki",       "lat": 60.1699,  "lon": 24.9384,  "acc": 20},
        {"city": "Tampere",        "lat": 61.4978,  "lon": 23.7610,  "acc": 30},
    ],
    "CH": [
        {"city": "Zurich",         "lat": 47.3769,  "lon": 8.5417,   "acc": 15},
        {"city": "Geneva",         "lat": 46.2044,  "lon": 6.1432,   "acc": 20},
    ],
    "AT": [
        {"city": "Vienna",         "lat": 48.2082,  "lon": 16.3738,  "acc": 20},
        {"city": "Salzburg",       "lat": 47.8095,  "lon": 13.0550,  "acc": 30},
    ],
    "BE": [
        {"city": "Brussels",       "lat": 50.8503,  "lon": 4.3517,   "acc": 20},
        {"city": "Antwerp",        "lat": 51.2194,  "lon": 4.4025,   "acc": 25},
    ],
    "PT": [
        {"city": "Lisbon",         "lat": 38.7223,  "lon": -9.1393,  "acc": 25},
        {"city": "Porto",          "lat": 41.1579,  "lon": -8.6291,  "acc": 30},
    ],
    "IE": [
        {"city": "Dublin",         "lat": 53.3498,  "lon": -6.2603,  "acc": 25},
        {"city": "Cork",           "lat": 51.8985,  "lon": -8.4756,  "acc": 35},
    ],
    "NZ": [
        {"city": "Auckland",       "lat": -36.8485, "lon": 174.7633, "acc": 30},
        {"city": "Wellington",     "lat": -41.2865, "lon": 174.7762, "acc": 35},
    ],
    "ZA": [
        {"city": "Johannesburg",   "lat": -26.2041, "lon": 28.0473,  "acc": 40},
        {"city": "Cape Town",      "lat": -33.9249, "lon": 18.4241,  "acc": 35},
    ],
    "AE": [
        {"city": "Dubai",          "lat": 25.2048,  "lon": 55.2708,  "acc": 20},
        {"city": "Abu Dhabi",      "lat": 24.4539,  "lon": 54.3773,  "acc": 25},
    ],
    "TR": [
        {"city": "Istanbul",       "lat": 41.0082,  "lon": 28.9784,  "acc": 30},
        {"city": "Ankara",         "lat": 39.9334,  "lon": 32.8597,  "acc": 35},
    ],
    "RU": [
        {"city": "Moscow",         "lat": 55.7558,  "lon": 37.6173,  "acc": 30},
        {"city": "St Petersburg",  "lat": 59.9311,  "lon": 30.3609,  "acc": 35},
    ],
    "TH": [
        {"city": "Bangkok",        "lat": 13.7563,  "lon": 100.5018, "acc": 35},
        {"city": "Chiang Mai",     "lat": 18.7883,  "lon": 98.9853,  "acc": 45},
    ],
    "PH": [
        {"city": "Manila",         "lat": 14.5995,  "lon": 120.9842, "acc": 40},
        {"city": "Cebu",           "lat": 10.3157,  "lon": 123.8854, "acc": 45},
    ],
    "ID": [
        {"city": "Jakarta",        "lat": -6.2088,  "lon": 106.8456, "acc": 45},
        {"city": "Surabaya",       "lat": -7.2575,  "lon": 112.7521, "acc": 50},
    ],
    "MY": [
        {"city": "Kuala Lumpur",   "lat": 3.1390,   "lon": 101.6869, "acc": 30},
        {"city": "Penang",         "lat": 5.4164,   "lon": 100.3327, "acc": 35},
    ],
    "VN": [
        {"city": "Ho Chi Minh",    "lat": 10.8231,  "lon": 106.6297, "acc": 40},
        {"city": "Hanoi",          "lat": 21.0278,  "lon": 105.8342, "acc": 45},
    ],
    "CL": [
        {"city": "Santiago",       "lat": -33.4489, "lon": -70.6693, "acc": 35},
        {"city": "Valparaíso",     "lat": -33.0472, "lon": -71.6127, "acc": 40},
    ],
    "CO": [
        {"city": "Bogotá",         "lat": 4.7110,   "lon": -74.0721, "acc": 40},
        {"city": "Medellín",       "lat": 6.2476,   "lon": -75.5658, "acc": 45},
    ],
    "AR": [
        {"city": "Buenos Aires",   "lat": -34.6037, "lon": -58.3816, "acc": 30},
        {"city": "Córdoba",        "lat": -31.4201, "lon": -64.1888, "acc": 40},
    ],
    "EG": [
        {"city": "Cairo",          "lat": 30.0444,  "lon": 31.2357,  "acc": 45},
        {"city": "Alexandria",     "lat": 31.2001,  "lon": 29.9187,  "acc": 50},
    ],
    "NG": [
        {"city": "Lagos",          "lat": 6.5244,   "lon": 3.3792,   "acc": 55},
        {"city": "Abuja",          "lat": 9.0579,   "lon": 7.4951,   "acc": 60},
    ],
    "KE": [
        {"city": "Nairobi",        "lat": -1.2921,  "lon": 36.8219,  "acc": 50},
        {"city": "Mombasa",        "lat": -4.0435,  "lon": 39.6682,  "acc": 55},
    ],
    "IL": [
        {"city": "Tel Aviv",       "lat": 32.0853,  "lon": 34.7818,  "acc": 20},
        {"city": "Jerusalem",      "lat": 31.7683,  "lon": 35.2137,  "acc": 25},
    ],
    "SA": [
        {"city": "Riyadh",         "lat": 24.7136,  "lon": 46.6753,  "acc": 35},
        {"city": "Jeddah",         "lat": 21.4858,  "lon": 39.1925,  "acc": 40},
    ],
    "PK": [
        {"city": "Karachi",        "lat": 24.8607,  "lon": 67.0011,  "acc": 55},
        {"city": "Lahore",         "lat": 31.5204,  "lon": 74.3587,  "acc": 50},
    ],
    "BD": [
        {"city": "Dhaka",          "lat": 23.8103,  "lon": 90.4125,  "acc": 60},
        {"city": "Chittagong",     "lat": 22.3569,  "lon": 91.7832,  "acc": 65},
    ],
    "TW": [
        {"city": "Taipei",         "lat": 25.0330,  "lon": 121.5654, "acc": 20},
        {"city": "Kaohsiung",      "lat": 22.6273,  "lon": 120.3014, "acc": 25},
    ],
    "HK": [
        {"city": "Hong Kong",      "lat": 22.3193,  "lon": 114.1694, "acc": 15},
    ],
    "RO": [
        {"city": "Bucharest",      "lat": 44.4268,  "lon": 26.1025,  "acc": 30},
        {"city": "Cluj-Napoca",    "lat": 46.7712,  "lon": 23.6236,  "acc": 35},
    ],
    "CZ": [
        {"city": "Prague",         "lat": 50.0755,  "lon": 14.4378,  "acc": 20},
        {"city": "Brno",           "lat": 49.1951,  "lon": 16.6068,  "acc": 30},
    ],
    "HU": [
        {"city": "Budapest",       "lat": 47.4979,  "lon": 19.0402,  "acc": 25},
        {"city": "Debrecen",       "lat": 47.5316,  "lon": 21.6273,  "acc": 35},
    ],
    "GR": [
        {"city": "Athens",         "lat": 37.9838,  "lon": 23.7275,  "acc": 25},
        {"city": "Thessaloniki",   "lat": 40.6401,  "lon": 22.9444,  "acc": 30},
    ],
}


def gps_for_country(country_code: str, seed: int = 0) -> dict:
    """Pick a GPS coordinate for a given ISO country code.

    Returns dict with keys: city, lat, lon, acc.
    Adds deterministic jitter (±0.005° ≈ 500m) seeded from the identity
    so the same identity always reports the same approximate location.
    If country not found, falls back to US coordinates.
    """
    import random as _rnd
    cc = country_code.upper()
    entries = GPS_COORDINATES.get(cc, GPS_COORDINATES.get("US", []))
    if not entries:
        return {"city": "New York", "lat": 40.7128, "lon": -74.0060, "acc": 30}
    # Deterministic city pick
    r = _rnd.Random(seed)
    entry = r.choice(entries)
    # Add deterministic jitter: ±0.005° latitude/longitude (≈500m urban spread)
    lat_jitter = (r.random() - 0.5) * 0.01   # -0.005 to +0.005
    lon_jitter = (r.random() - 0.5) * 0.01
    # Accuracy jitter: ±20% of base accuracy
    acc_jitter = entry["acc"] * (0.8 + r.random() * 0.4)
    return {
        "city": entry["city"],
        "lat": round(entry["lat"] + lat_jitter, 6),
        "lon": round(entry["lon"] + lon_jitter, 6),
        "acc": round(acc_jitter, 1),
    }


def carrier_for_country(country_code: str, seed: int = 0) -> dict[str, str]:
    """Pick a carrier entry for a given ISO country code.

    Returns dict with keys: name, mcc_mnc, tz, country.
    Uses the seed for deterministic selection (e.g., hash of instance name).
    If country not found, returns a generic US T-Mobile entry.
    """
    import random as _rnd
    cc = country_code.upper()
    entries = CARRIER_DB.get(cc, CARRIER_DB.get("US", []))
    if not entries:
        return {"name": "T-Mobile", "mcc_mnc": "310260",
                "tz": "America/New_York", "country": "US"}
    # Deterministic pick based on seed
    r = _rnd.Random(seed)
    entry = r.choice(entries)
    return {**entry, "country": cc}


# ─── Profile ────────────────────────────────────────────────────────────────


@dataclass
class DeviceProfile:
    """Device identity values used to generate the JS spoofing payload."""
    user_agent: str = ""
    model: str = ""
    brand: str = ""
    manufacturer: str = ""
    device: str = ""
    platform: str = "Linux armv8l"
    cpu_cores: int = 8
    ram_gb: int = 8
    build_id: str = ""
    build_fingerprint: str = ""
    build_version_release: str = "13"
    build_version_sdk: str = "33"
    screen_width: int = 1080
    screen_height: int = 2400
    screen_density: int = 420
    gl_renderer: str = "Adreno (TM) 730"
    gl_vendor: str = "Qualcomm"
    gl_version: str = "OpenGL ES 3.2"
    languages: str = "en-US"
    timezone: str = "America/New_York"
    carrier_country: str = ""  # From profile: "gb", "us", etc.
    chrome_version: str = ""  # Auto-detected: "133.0.6943.137" etc.
    chrome_major: str = ""    # Auto-detected: "133" etc.


def _not_a_brand_version(chrome_major: str) -> str:
    """Compute the 'Not_A Brand' greased version for Sec-CH-UA client hints.

    The greased brand version rotates periodically with Chrome releases.
    Mapping derived from real Sec-CH-UA headers across Chrome releases.
    """
    try:
        major = int(chrome_major)
    except (ValueError, TypeError):
        return "24"
    # Chromium greasing rotations (verified from real browser output):
    if major >= 124:
        return "24"
    elif major >= 120:
        return "8"
    else:
        return "99"


def _read_chrome_version(adb_exe: str, serial: str) -> tuple[str, str]:
    """Detect the installed Chrome version on the device via ADB.

    Queries the package manager for com.android.chrome's versionName.
    Returns (full_version, major_version) — e.g. ("133.0.6943.137", "133").
    Falls back to a reasonable default if Chrome isn't found.
    """
    default_full = "133.0.6943.137"
    default_major = "133"

    try:
        # dumpsys package is the most reliable way to get the version
        cp = subprocess.run(
            [adb_exe, "-s", serial, "shell",
             "dumpsys package com.android.chrome | grep versionName"],
            capture_output=True, text=True, timeout=8,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            # Output looks like: "    versionName=133.0.6943.137"
            for line in cp.stdout.strip().splitlines():
                line = line.strip()
                if "versionName=" in line:
                    ver = line.split("versionName=", 1)[1].strip()
                    if ver and ver[0].isdigit():
                        major = ver.split(".")[0]
                        return (ver, major)
    except Exception:
        pass

    # Fallback: try pm list packages -f to get the APK path, then aapt
    try:
        cp = subprocess.run(
            [adb_exe, "-s", serial, "shell",
             "pm dump com.android.chrome 2>/dev/null | head -20"],
            capture_output=True, text=True, timeout=8,
        )
        if cp.returncode == 0:
            for line in cp.stdout.strip().splitlines():
                if "versionName" in line:
                    parts = line.strip().split("=")
                    if len(parts) >= 2:
                        ver = parts[-1].strip()
                        if ver and ver[0].isdigit():
                            major = ver.split(".")[0]
                            return (ver, major)
    except Exception:
        pass

    return (default_full, default_major)


def read_device_profile(adb_exe: str, serial: str) -> DeviceProfile:
    """Read the active device profile from a running instance via ADB."""
    profile = DeviceProfile()

    # Try multiple config paths in order
    cmd = (
        f"su -c \"cat {READABLE_CONF_PATH_TMP} 2>/dev/null "
        f"|| cat {READABLE_CONF_PATH} 2>/dev/null "
        f"|| cat {ACTIVE_CONF_PATH} 2>/dev/null\""
    )
    try:
        cp = subprocess.run(
            [adb_exe, "-s", serial, "shell", cmd],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return profile

    if cp.returncode != 0 or not cp.stdout.strip():
        return profile

    kv: dict[str, str] = {}
    for line in cp.stdout.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        eq = line.find("=")
        if eq > 0:
            key = line[:eq].strip()
            val = line[eq + 1 :].strip().strip('"').strip("'")
            kv[key] = val

    # Map PROFILE_* keys → DeviceProfile fields
    profile.model = kv.get("PROFILE_MODEL", profile.model)
    profile.brand = kv.get("PROFILE_BRAND", profile.brand)
    profile.manufacturer = kv.get("PROFILE_MANUFACTURER", profile.manufacturer)
    profile.device = kv.get("PROFILE_DEVICE", profile.device)
    profile.build_id = kv.get("PROFILE_BUILD_ID", profile.build_id)
    profile.build_fingerprint = kv.get("PROFILE_BUILD_FINGERPRINT", profile.build_fingerprint)
    profile.build_version_release = kv.get("PROFILE_BUILD_VERSION_RELEASE", profile.build_version_release)
    profile.build_version_sdk = kv.get("PROFILE_BUILD_VERSION_SDK", profile.build_version_sdk)
    profile.gl_renderer = kv.get("PROFILE_GL_RENDERER", profile.gl_renderer)
    profile.gl_vendor = kv.get("PROFILE_GL_VENDOR", profile.gl_vendor)
    profile.gl_version = kv.get("PROFILE_GL_VERSION_STRING", kv.get("PROFILE_GL_VERSION", profile.gl_version))
    profile.timezone = kv.get("PROFILE_TIMEZONE", profile.timezone)
    profile.carrier_country = kv.get("PROFILE_CARRIER_COUNTRY", profile.carrier_country)

    try:
        profile.cpu_cores = int(kv.get("PROFILE_CPU_CORES", str(profile.cpu_cores)))
    except ValueError:
        pass
    try:
        profile.ram_gb = int(kv.get("PROFILE_RAM_GB", str(profile.ram_gb)))
    except ValueError:
        pass
    try:
        profile.screen_width = int(kv.get("PROFILE_SCREEN_WIDTH", str(profile.screen_width)))
    except ValueError:
        pass
    try:
        profile.screen_height = int(kv.get("PROFILE_SCREEN_HEIGHT", str(profile.screen_height)))
    except ValueError:
        pass
    try:
        profile.screen_density = int(kv.get("PROFILE_SCREEN_DENSITY", str(profile.screen_density)))
    except ValueError:
        pass

    # Detect installed Chrome version on the device
    chrome_full, chrome_major = _read_chrome_version(adb_exe, serial)
    profile.chrome_version = chrome_full
    profile.chrome_major = chrome_major

    # Build user agent from profile data if not explicitly set
    ua_raw = kv.get("webview.user_agent", "")
    if ua_raw:
        profile.user_agent = ua_raw
    else:
        android_ver = profile.build_version_release or "13"
        model = profile.model or "Pixel 7"
        bid = profile.build_id or "TQ3A.230901.001"
        profile.user_agent = (
            f"Mozilla/5.0 (Linux; Android {android_ver}; {model} Build/{bid}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_full} "
            "Mobile Safari/537.36"
        )

    return profile


# ─── JS Payload Generator ───────────────────────────────────────────────────


def _seed_from_fingerprint(fp: str) -> int:
    """Derive a 32-bit seed from build fingerprint for deterministic noise."""
    h = hashlib.md5(fp.encode("utf-8", errors="replace")).hexdigest()
    return int(h[:8], 16)


def _gpu_params_for_renderer(gl_renderer: str, android_sdk: int) -> dict:
    """Return WebGL numeric parameter overrides for a given GPU renderer.

    Chrome on Android uses ANGLE which clamps certain values:
    - Android < 14 (SDK < 34): MAX_TEXTURE_SIZE clamped to 4096
    - Android >= 14 (SDK >= 34): MAX_TEXTURE_SIZE clamped to 8192
    - MAX_3D_TEXTURE_SIZE / MAX_ARRAY_TEXTURE_LAYERS clamped to 1024 on all Android
    - ALIASED_LINE_WIDTH_RANGE always [1,1] via ANGLE

    Data sourced from opengles.gpuinfo.org device reports and Chromium ANGLE source.
    """
    # ANGLE texture size cap depends on Android version
    tex_cap = 4096 if android_sdk < 34 else 8192

    # ── Qualcomm Adreno family ──
    # All Adreno 6xx/7xx share identical native driver limits:
    #   native MAX_TEXTURE_SIZE=16384, MAX_3D=2048, MAX_ARRAY_LAYERS=2048
    #   MAX_TEXTURE_IMAGE_UNITS=16, MAX_COMBINED=96, MAX_VERTEX_TEX=16
    #   MAX_FRAG_UNIFORM_VECTORS=256, MAX_VERT_UNIFORM_VECTORS=256
    adreno_base = {
        "MAX_TEXTURE_SIZE": tex_cap,
        "MAX_CUBE_MAP_TEXTURE_SIZE": tex_cap,
        "MAX_RENDERBUFFER_SIZE": tex_cap,
        "MAX_VIEWPORT_DIMS": [tex_cap, tex_cap],
        "MAX_3D_TEXTURE_SIZE": 1024,
        "MAX_ARRAY_TEXTURE_LAYERS": 1024,
        "ALIASED_POINT_SIZE_RANGE": [1, 1024],
        "ALIASED_LINE_WIDTH_RANGE": [1, 1],
        "MAX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 96,
        "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_FRAGMENT_UNIFORM_VECTORS": 256,
        "MAX_VERTEX_UNIFORM_VECTORS": 256,
        "MAX_VERTEX_ATTRIBS": 16,
        "MAX_VERTEX_UNIFORM_COMPONENTS": 16384,
        "MAX_VERTEX_UNIFORM_BLOCKS": 16,
        "MAX_VERTEX_OUTPUT_COMPONENTS": 124,
        "MAX_VARYING_VECTORS": 31,
        "MAX_VARYING_COMPONENTS": 120,
        "MAX_FRAGMENT_UNIFORM_COMPONENTS": 16384,
        "MAX_FRAGMENT_UNIFORM_BLOCKS": 16,
        "MAX_FRAGMENT_INPUT_COMPONENTS": 124,
        "MAX_DRAW_BUFFERS": 8,
        "MAX_COLOR_ATTACHMENTS": 8,
        "MAX_SAMPLES": 4,
        "MAX_UNIFORM_BUFFER_BINDINGS": 72,
        "MAX_UNIFORM_BLOCK_SIZE": 65536,
        "MAX_COMBINED_UNIFORM_BLOCKS": 60,
        "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS": 507904,
        "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS": 507904,
        "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS": 128,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS": 4,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS": 4,
        "MIN_PROGRAM_TEXEL_OFFSET": -8,
        "MAX_PROGRAM_TEXEL_OFFSET": 7,
        "MAX_TEXTURE_LOD_BIAS": 4,
        "UNIFORM_BUFFER_OFFSET_ALIGNMENT": 16,
    }

    # ── ARM Mali Valhall family (G77, G78, G710) ──
    # All Valhall GPUs share the same driver; native MAX_TEXTURE_SIZE=16383
    # Much higher texture/uniform limits than Adreno
    mali_valhall = {
        "MAX_TEXTURE_SIZE": tex_cap,
        "MAX_CUBE_MAP_TEXTURE_SIZE": tex_cap,
        "MAX_RENDERBUFFER_SIZE": tex_cap,
        "MAX_VIEWPORT_DIMS": [tex_cap, tex_cap],
        "MAX_3D_TEXTURE_SIZE": 1024,
        "MAX_ARRAY_TEXTURE_LAYERS": 1024,
        "ALIASED_POINT_SIZE_RANGE": [1, 1024],
        "ALIASED_LINE_WIDTH_RANGE": [1, 1],
        "MAX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 80,
        "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 16,
        "MAX_FRAGMENT_UNIFORM_VECTORS": 1024,
        "MAX_VERTEX_UNIFORM_VECTORS": 1024,
        "MAX_VERTEX_ATTRIBS": 16,
        "MAX_VERTEX_UNIFORM_COMPONENTS": 16384,
        "MAX_VERTEX_UNIFORM_BLOCKS": 16,
        "MAX_VERTEX_OUTPUT_COMPONENTS": 124,
        "MAX_VARYING_VECTORS": 31,
        "MAX_VARYING_COMPONENTS": 120,
        "MAX_FRAGMENT_UNIFORM_COMPONENTS": 16384,
        "MAX_FRAGMENT_UNIFORM_BLOCKS": 16,
        "MAX_FRAGMENT_INPUT_COMPONENTS": 124,
        "MAX_DRAW_BUFFERS": 8,
        "MAX_COLOR_ATTACHMENTS": 8,
        "MAX_SAMPLES": 4,
        "MAX_UNIFORM_BUFFER_BINDINGS": 72,
        "MAX_UNIFORM_BLOCK_SIZE": 65536,
        "MAX_COMBINED_UNIFORM_BLOCKS": 60,
        "MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS": 507904,
        "MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS": 507904,
        "MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS": 128,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS": 4,
        "MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS": 4,
        "MIN_PROGRAM_TEXEL_OFFSET": -8,
        "MAX_PROGRAM_TEXEL_OFFSET": 7,
        "MAX_TEXTURE_LOD_BIAS": 4,
        "UNIFORM_BUFFER_OFFSET_ALIGNMENT": 16,
    }

    # ── ARM Mali Bifrost family (G68) ──
    # Same as Valhall for most values (same driver codebase)
    mali_bifrost = dict(mali_valhall)

    # Renderer → params lookup (covers ALL 10 GPU strings from the 33 profiles)
    gpu_db = {
        # Qualcomm Adreno 6xx
        "Adreno (TM) 618": adreno_base,
        "Adreno (TM) 619": adreno_base,
        "Adreno (TM) 620": adreno_base,
        "Adreno (TM) 650": adreno_base,
        "Adreno (TM) 660": adreno_base,
        # Qualcomm Adreno 7xx
        "Adreno (TM) 725": adreno_base,
        "Adreno (TM) 730": adreno_base,
        "Adreno (TM) 740": adreno_base,
        # ARM Mali Valhall
        "Mali-G710 MC10": mali_valhall,
        "Mali-G78 MP14": mali_valhall,
        "Mali-G77 MP11": mali_valhall,
        # ARM Mali Bifrost (A34, A54)
        "Mali-G68 MC4": mali_bifrost,
        "Mali-G68 MP5": mali_bifrost,
    }

    return gpu_db.get(gl_renderer, adreno_base)


def _shader_timing_for_renderer(gl_renderer: str) -> dict:
    """Return shader compilation timing profile for a given GPU renderer.

    These values simulate the timing characteristics of real mobile GPU shader
    compilers. The key insight is that on a real phone, shader compilation
    goes through:  GLSL → ANGLE validator → native driver → GPU ISA binary
    On an emulator, it goes through:  GLSL → ANGLE → HLSL/SPIRV translation
    → desktop GPU driver — producing fundamentally different timing ratios.

    We intercept compileShader/linkProgram timing and inject calibrated delays
    to make the emulator's timing profile match the target mobile GPU.

    Timing data sourced from USENIX Security '19 "Rendered Private" paper,
    Chromium bug reports, and mobile GPU shader compiler documentation.
    """
    # Adreno 7xx: flagship Qualcomm, fast native compiler
    adreno_7xx = {
        "base_compile_ms": 0.12,   # Trivial shader compile time
        "base_link_ms": 0.20,      # Trivial shader link time
        "complexity_scale": 15.0,  # Max multiplier for complex shaders
        "link_to_compile": 1.8,    # link time ≈ 1.8× compile time
        "jitter_pct": 0.15,        # ±15% per-measurement variance
    }

    # Adreno 6xx: mid-range Qualcomm, slightly slower compiler
    adreno_6xx = {
        "base_compile_ms": 0.18,
        "base_link_ms": 0.30,
        "complexity_scale": 12.0,
        "link_to_compile": 1.6,
        "jitter_pct": 0.18,
    }

    # Mali Valhall (G710, G78, G77): ARM's more conservative compiler
    mali_valhall = {
        "base_compile_ms": 0.22,
        "base_link_ms": 0.32,
        "complexity_scale": 10.0,
        "link_to_compile": 1.5,
        "jitter_pct": 0.12,  # Mali has tighter variance
    }

    # Mali Bifrost (G68): budget ARM GPUs
    mali_bifrost = {
        "base_compile_ms": 0.28,
        "base_link_ms": 0.40,
        "complexity_scale": 8.0,
        "link_to_compile": 1.4,
        "jitter_pct": 0.14,
    }

    timing_db = {
        "Adreno (TM) 618": adreno_6xx,
        "Adreno (TM) 619": adreno_6xx,
        "Adreno (TM) 620": adreno_6xx,
        "Adreno (TM) 650": adreno_6xx,
        "Adreno (TM) 660": adreno_6xx,
        "Adreno (TM) 725": adreno_7xx,
        "Adreno (TM) 730": adreno_7xx,
        "Adreno (TM) 740": adreno_7xx,
        "Mali-G710 MC10": mali_valhall,
        "Mali-G78 MP14": mali_valhall,
        "Mali-G77 MP11": mali_valhall,
        "Mali-G68 MC4": mali_bifrost,
        "Mali-G68 MP5": mali_bifrost,
    }

    return timing_db.get(gl_renderer, adreno_7xx)


def _dpr_for_device(model: str, screen_width: int, screen_density: int) -> float:
    """Determine the actual devicePixelRatio Chrome uses on a given device.

    Chrome on Android derives DPR from the system display density (ro.sf.lcd_density)
    divided by the baseline 160dpi.  However, our profile configs store various density
    values (some ro.sf.lcd_density, some physical PPI) so we use a precise lookup.

    Verified DPR values from real devices (browserleaks, yesviz, blisk, screensizechecker):

      1440px wide (all devices):  DPR 3.5 → 411 CSS px
        OnePlus 9 Pro (450dpi), OnePlus 11 (480dpi), Pixel 7 Pro (512dpi),
        Samsung Note 20 Ultra (560dpi), S21 Ultra (560dpi), S23 Ultra (450dpi),
        Xiaomi 13 Pro (560dpi), Xiaomi Mi 11 (560dpi)

      1644px wide (Sony Xperia 4K):  DPR 4.0 → 411 CSS px
        Sony Xperia 1 III (420dpi), Sony Xperia 1 V (420dpi)

      1080px wide — varies by density bracket:
        ≤400dpi:  DPR 2.5    → 432 CSS px  (Moto Edge 20P/40P, G84, Nokia X20)
        401-420:  DPR 2.625  → 411 CSS px  (Pixel 4a5G/7/7a, OP9, A52/A72, Xiaomi 13, Nothing Phone 2)
        421-440:  DPR 2.75   → 393 CSS px  (Pixel 5, POCO F3/F5, Redmi Note 10 Pro)
        441-450:  DPR 2.8125 → 384 CSS px  (Samsung A34/A54, S23+, Xperia 5V)
        451-480:  DPR 3.0    → 360 CSS px  (Samsung S21, S23)
        >480:     DPR 3.5    → 309 CSS px  (OnePlus Nord 3 at 560dpi)
    """
    # ── 1. Model-specific overrides (for profiles with ambiguous density values) ──
    # These handle cases where profile density doesn't cleanly map via brackets.
    _model_lower = model.lower() if model else ""
    _MODEL_DPR = {
        # Pixel 4a 5G: profile has density 411 (PPI) but real lcd_density is 420 → DPR 2.625
        "pixel 4a (5g)": 2.625,
    }
    for key, dpr in _MODEL_DPR.items():
        if key in _model_lower:
            return dpr

    # ── 2. Width-based primary lookup ──
    if screen_width == 1440:
        # All 1440px-wide phones use DPR 3.5 regardless of density
        # (WQHD+ Samsung, OnePlus, Pixel Pro, Xiaomi flagships)
        return 3.5

    if screen_width == 1644:
        # Sony Xperia 4K (1644 × 3840): DPR 4.0 → 411 CSS px
        return 4.0

    if screen_width == 1240:
        # OnePlus Nord 3 real resolution (1240 × 2772): DPR 3.5 → 354 CSS px
        # (If profile has correct 1240 width)
        return 3.5

    if screen_width == 1080:
        # ── Precise density-bracket mapping for 1080px devices ──
        # Each bracket corresponds to the real ro.sf.lcd_density / 160 values
        # observed on actual devices, verified against browser-reported DPR.
        if screen_density <= 400:
            # 400dpi → DPR 2.5 → 432 CSS px
            # Motorola Edge 20 Pro, Edge 40 Pro, Moto G84 5G, Nokia X20
            return 2.5
        elif screen_density <= 420:
            # 411-420dpi → DPR 2.625 → 411 CSS px
            # Pixel 4a 5G (411), Pixel 7 (420), Pixel 7a (420),
            # OnePlus 9 (420), Nothing Phone 2 (420),
            # Samsung A52 (420), A72 (420), Xiaomi 13 (420)
            return 2.625
        elif screen_density <= 440:
            # 440dpi → DPR 2.75 → 393 CSS px
            # Pixel 5 (440), POCO F3 (440), POCO F5 (440), Redmi Note 10 Pro (440)
            return 2.75
        elif screen_density <= 450:
            # 450dpi → DPR 2.8125 → 384 CSS px
            # Samsung A34 (450), A54 (450), S23+ (450), Sony Xperia 5V (450)
            return 2.8125
        elif screen_density <= 480:
            # 480dpi → DPR 3.0 → 360 CSS px
            # Samsung S21 (480), Samsung S23 (480)
            return 3.0
        else:
            # >480dpi on 1080px (unusual, e.g., OnePlus Nord 3 at 560dpi)
            # 560/160 = 3.5 → 309 CSS px
            return 3.5

    # ── 3. Fallback for unknown widths ──
    # Find a known DPR that produces a standard CSS viewport width (360-432px).
    known_dprs = [2.0, 2.25, 2.5, 2.625, 2.75, 2.8125, 3.0, 3.5, 3.75, 4.0]
    target_css_min, target_css_max = 360, 435

    candidates = []
    for d in known_dprs:
        css_w = screen_width / d
        if target_css_min <= css_w <= target_css_max:
            candidates.append((abs(d - screen_density / 160.0), d))

    if candidates:
        candidates.sort()
        return candidates[0][1]

    # Last resort: snap to nearest known DPR
    raw_dpr = screen_density / 160.0
    best = min(known_dprs, key=lambda d: abs(d - raw_dpr))
    return best


def generate_spoof_payload(profile: DeviceProfile) -> str:
    """Build the comprehensive JS fingerprint-spoofing payload."""
    dpr = _dpr_for_device(profile.model, profile.screen_width, profile.screen_density)
    # deviceMemory must be a power of 2 per Web spec (0.25, 0.5, 1, 2, 4, 8)
    raw_mem = profile.ram_gb
    _mem_powers = [0.25, 0.5, 1, 2, 4, 8]
    device_memory = min(_mem_powers, key=lambda p: abs(p - raw_mem))
    seed = _seed_from_fingerprint(profile.build_fingerprint)
    lang_primary = profile.languages.split(",")[0].strip() if "," in profile.languages else profile.languages

    # ── Screen metrics in CSS pixels (what a real phone's Chrome reports) ──
    # Status bar height: 24dp ≈ 24 CSS px (dp ≈ CSS px on mobile)
    status_bar_dp = 24
    # Navigation bar: 48dp for 3-button nav, 16dp for gesture nav; use 48 conservatively
    nav_bar_dp = 48
    # Chrome toolbar (omnibox): 56dp
    chrome_toolbar_dp = 56

    # Physical pixel heights for each bar (for screen.availHeight calculation)
    status_bar_px = round(status_bar_dp * dpr)
    nav_bar_px = round(nav_bar_dp * dpr)

    # screen.availHeight = physical height - status bar physical pixels
    avail_height = profile.screen_height - status_bar_px

    # CSS pixel viewport dimensions (what window.innerWidth/Height report)
    css_viewport_w = round(profile.screen_width / dpr)
    # innerHeight = total CSS height - status bar - nav bar - chrome toolbar
    css_total_h = round(profile.screen_height / dpr)
    css_viewport_h = css_total_h - status_bar_dp - nav_bar_dp - chrome_toolbar_dp
    # outerWidth/Height: on Android Chrome, outer = inner (no window decorations)
    css_outer_w = css_viewport_w
    css_outer_h = css_viewport_h

    # Get GPU-accurate WebGL parameters
    android_sdk = 30
    try:
        android_sdk = int(profile.build_version_sdk)
    except (ValueError, TypeError):
        pass
    gpu_params = _gpu_params_for_renderer(profile.gl_renderer, android_sdk)

    # Serialize GPU params to JS object literal
    gpu_js_entries = []
    for key, val in gpu_params.items():
        if isinstance(val, list):
            gpu_js_entries.append(f"    '{key}': [{', '.join(str(v) for v in val)}]")
        else:
            gpu_js_entries.append(f"    '{key}': {val}")
    gpu_params_js = "{\n" + ",\n".join(gpu_js_entries) + "\n  }"

    # Get GPU-specific shader compilation timing profile
    shader_timing = _shader_timing_for_renderer(profile.gl_renderer)

    # Chrome version strings (auto-detected from device, see _read_chrome_version)
    chrome_full = profile.chrome_version or "133.0.6943.137"
    chrome_major = profile.chrome_major or "133"
    # The "Not_A Brand" greased version rotates with Chrome major versions
    not_a_brand_ver = _not_a_brand_version(chrome_major)

    return f"""(function() {{
'use strict';

// ════════════════════════════════════════════════════════════
// 0. CDP ANTI-DETECTION — must run FIRST, before any other code
// ════════════════════════════════════════════════════════════

// 0a. Remove cdc_ and automation artifacts injected by Chrome DevTools
// Chrome injects window.cdc_* properties when a DevTools client connects.
// These are the #1 most well-known CDP fingerprinting vector.
(function _cleanCDP() {{
    // Clean window
    try {{
        Object.keys(window).forEach(function(k) {{
            if (/^cdc_|^__webdriver|^__selenium|^__driver|^\$cdc_/.test(k)) {{
                try {{ delete window[k]; }} catch(e) {{}}
            }}
        }});
    }} catch(e) {{}}
    // Clean document
    try {{
        Object.keys(document).forEach(function(k) {{
            if (/^cdc_|^__webdriver|^\$cdc_/.test(k)) {{
                try {{ delete document[k]; }} catch(e) {{}}
            }}
        }});
    }} catch(e) {{}}
    // Re-run periodically — CDP may re-inject these at any time
    setTimeout(_cleanCDP, 500);
}})();

// 0b. navigator.webdriver — force to false (what real Android Chrome returns)
// CDP sets this to true when DevTools connects. Real phones return false.
// undefined is MORE suspicious than false — real Chrome always has this property.
try {{
    delete Navigator.prototype.webdriver;
    var _getWebdriver = function() {{ return false; }};
    _storeOrig(_getWebdriver, 'get webdriver');
    Object.defineProperty(Navigator.prototype, 'webdriver', {{
        get: _getWebdriver,
        configurable: true,
        enumerable: true,
    }});
}} catch(e) {{}}

// 0c. Sanitize Error stack traces — remove CDP injection frame references
// Scripts injected via Page.addScriptToEvaluateOnNewDocument leave traces
// like "evaluateOnNewDocument" or "__puppeteer_evaluation_script__" in
// Error stack traces. Cloudflare's bot detection inspects these.
try {{
    var _origStackGetter = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
    if (_origStackGetter && _origStackGetter.get) {{
        var _filteredStackGetter = function() {{
            var s = _origStackGetter.get.call(this);
            if (typeof s === 'string') {{
                return s.split('\\n').filter(function(l) {{
                    return !/evaluateOnNewDocument|__puppeteer|__selenium|extensions::SafeBuiltins/.test(l);
                }}).join('\\n');
            }}
            return s;
        }};
        _storeOrig(_filteredStackGetter, 'get stack');
        Object.defineProperty(Error.prototype, 'stack', {{
            get: _filteredStackGetter,
            set: _origStackGetter.set,
            configurable: true,
        }});
    }}
}} catch(e) {{}}

// 0d. Notification.permission — must be 'denied' (Android Chrome default)
// Under CDP automation this can return 'default' instead of 'denied',
// which is a known Cloudflare detection signal.
try {{
    var _getNotifPerm = function() {{ return 'denied'; }};
    _storeOrig(_getNotifPerm, 'get permission');
    Object.defineProperty(Notification, 'permission', {{
        get: _getNotifPerm,
        configurable: true,
    }});
}} catch(e) {{}}

// 0e. Ensure window.chrome object matches real Android Chrome
// CDP may modify the window.chrome object or add desktop-only properties.
try {{
    if (!window.chrome) window.chrome = {{}};
    if (!window.chrome.runtime) {{
        window.chrome.runtime = {{
            connect: function() {{}},
            sendMessage: function() {{}},
        }};
    }}
    // Desktop-only APIs should NOT exist on Android Chrome
    delete window.chrome.csi;
    delete window.chrome.loadTimes;
}} catch(e) {{}}

// 0f. console.debug integrity — CDP Runtime domain may alter console behavior
// Cloudflare checks console.debug.toString() returns native code signature
try {{
    var _consoleDebug = console.debug;
    if (_consoleDebug && /function/.test(Function.prototype.toString.call(_consoleDebug))) {{
        // Already looks native — leave it alone
    }}
}} catch(e) {{}}

// ── Deterministic PRNG for consistent-per-identity noise ──
var _s = {seed} >>> 0;
function _rng() {{ _s = (_s * 1664525 + 1013904223) >>> 0; return _s; }}
function _noiseF() {{ return ((_rng() / 0xFFFFFFFF) - 0.5) * 0.00008; }}

// ── Store originals for toString hardening ──
// MUST be declared before _def() and before any getters are created.
var _origFns = new Map();
function _storeOrig(fn, name) {{ _origFns.set(fn, name); }}

// ── Function.prototype.toString hardening ──
// MUST be installed BEFORE any getter is created, so that by the time
// Cloudflare's script runs, all registered getters return native-looking strings.
// Detection is ONE LINE: d.get.toString() — if it doesn't say "native code", bot.
var _origToStr = Function.prototype.toString;
var _newToStr = function() {{
    if (_origFns.has(this)) {{
        return 'function ' + _origFns.get(this) + '() {{ [native code] }}';
    }}
    return _origToStr.call(this);
}};
Function.prototype.toString = _newToStr;
// toString MUST protect itself — toString.toString() must also return native code
_origFns.set(_newToStr, 'toString');

// ── Helper: safe defineProperty with automatic toString registration ──
// Every getter created by _def() is registered with _storeOrig so that
// its .toString() returns "function get <prop>() {{ [native code] }}"
function _def(obj, prop, val) {{
    try {{
        var _getter = function() {{ return val; }};
        _storeOrig(_getter, 'get ' + prop);
        Object.defineProperty(obj, prop, {{
            get: _getter,
            configurable: false,
            enumerable: true,
        }});
    }} catch(e) {{}}
}}

// ── Capture original performance.now before any override ──
// Used by both shader timing (Section 3b) and timing mitigation (Section 7).
var _origPerfNow = Performance.prototype.now;

// ── SharedArrayBuffer availability ──
// Real Android Chrome always exposes SharedArrayBuffer (since Chrome 92).
// Cloudflare Turnstile checks typeof SharedArrayBuffer !== 'undefined'.
// If the BlueStacks Chrome build doesn't have it, polyfill the constructor.
if (typeof SharedArrayBuffer === 'undefined') {{
    Object.defineProperty(window, 'SharedArrayBuffer', {{
        value: function SharedArrayBuffer(length) {{
            return new ArrayBuffer(length);
        }},
        writable: true,
        configurable: true,
        enumerable: false,
    }});
    Object.defineProperty(SharedArrayBuffer, Symbol.hasInstance, {{
        value: function(instance) {{
            return instance instanceof ArrayBuffer;
        }},
    }});
}}

// ════════════════════════════════════════════════════════════
// 1. NAVIGATOR
// ════════════════════════════════════════════════════════════

var _nav = {{
    userAgent: {json.dumps(profile.user_agent)},
    appVersion: {json.dumps(profile.user_agent.replace("Mozilla/", "", 1))},
    platform: {json.dumps(profile.platform)},
    hardwareConcurrency: {profile.cpu_cores},
    deviceMemory: {device_memory},
    maxTouchPoints: 5,
    languages: Object.freeze([{json.dumps(lang_primary)}, 'en']),
    language: {json.dumps(lang_primary)},
    vendor: 'Google Inc.',
    product: 'Gecko',
    productSub: '20030107',
    pdfViewerEnabled: false,
    // webdriver is handled in Section 0b (set to false via registered getter)
}};
for (var _k in _nav) {{
    _def(Navigator.prototype, _k, _nav[_k]);
}}

// ── navigator.connection ──
try {{
    var _connProto = {{
        effectiveType: '4g',
        type: 'cellular',
        downlink: 10,
        rtt: 50,
        saveData: false,
        onchange: null,
    }};
    // Give it the correct prototype chain so instanceof/toString works
    // Each getter must be registered for toString hardening
    var _getEffType = function() {{ return _connProto.effectiveType; }};
    var _getConnType = function() {{ return _connProto.type; }};
    var _getDownlink = function() {{ return _connProto.downlink; }};
    var _getRtt = function() {{ return _connProto.rtt; }};
    var _getSaveData = function() {{ return _connProto.saveData; }};
    var _getOnchange = function() {{ return _connProto.onchange; }};
    _storeOrig(_getEffType, 'get effectiveType');
    _storeOrig(_getConnType, 'get type');
    _storeOrig(_getDownlink, 'get downlink');
    _storeOrig(_getRtt, 'get rtt');
    _storeOrig(_getSaveData, 'get saveData');
    _storeOrig(_getOnchange, 'get onchange');
    var _conn = Object.create(NetworkInformation.prototype, {{
        effectiveType: {{ get: _getEffType, enumerable: true }},
        type: {{ get: _getConnType, enumerable: true }},
        downlink: {{ get: _getDownlink, enumerable: true }},
        rtt: {{ get: _getRtt, enumerable: true }},
        saveData: {{ get: _getSaveData, enumerable: true }},
        onchange: {{ get: _getOnchange, set: function(v) {{ _connProto.onchange = v; }}, enumerable: true }},
    }});
    _def(Navigator.prototype, 'connection', _conn);
}} catch(e) {{
    // Fallback if NetworkInformation is not available
    try {{
        var _connFB = {{
            effectiveType: '4g', type: 'cellular', downlink: 10, rtt: 50,
            saveData: false, onchange: null,
            addEventListener: function() {{}}, removeEventListener: function() {{}},
            dispatchEvent: function() {{ return true; }},
        }};
        _def(Navigator.prototype, 'connection', _connFB);
    }} catch(e2) {{}}
}}

// ── navigator.getBattery() ──
// SYNC: Values aligned with LSPosed BatteryHooks.smali:
//   - Start level: 60-89% (seed-deterministic, same range as Java hooks)
//   - Drain rate: ~0.15%/min (matches BatteryHooks.DRAIN_RATE_PER_MINUTE)
//   - Status: discharging (matches BatteryHooks forced status=3)
//   - Temp equivalent: 24-25°C (240-250 tenths, same as Java hooks)
try {{
    var _battFn = Navigator.prototype.getBattery;
    // Deterministic start level: 60-89% (same range as BatteryHooks)
    var _battStartLevel = (60 + (_rng() % 30)) / 100;
    var _battStartTime = Date.now();
    // dischargingTime = time until 0% at 0.15%/min = level / 0.0025 per second
    var _battDischTime = Math.floor((_battStartLevel * 100) / 0.15 * 60);
    var _batt = {{
        charging: false,
        chargingTime: Infinity,
        dischargingTime: _battDischTime,
        level: _battStartLevel,
        onchargingchange: null,
        onchargingtimechange: null,
        ondischargingtimechange: null,
        onlevelchange: null,
        addEventListener: function(type, cb) {{ this['on' + type] = cb; }},
        removeEventListener: function(type) {{ this['on' + type] = null; }},
        dispatchEvent: function() {{ return true; }},
    }};
    // Live drain simulation (same 0.15%/min as BatteryHooks)
    // Floor at 15%: simulate "plugged in" to avoid unrealistically low battery on long sessions
    setInterval(function() {{
        var elapsed = (Date.now() - _battStartTime) / 60000; // minutes
        var drained = elapsed * 0.15 / 100; // 0.15% per minute
        var newLevel = Math.max(0.15, _battStartLevel - drained);
        if (newLevel <= 0.15 && !_batt.charging) {{
            // Simulate plugging in the phone when battery gets low
            _batt.charging = true;
            _batt.chargingTime = 3600; // ~1 hour to full
            _batt.dischargingTime = Infinity;
            if (_batt.onchargingchange) {{ try {{ _batt.onchargingchange(new Event('chargingchange')); }} catch(e2){{}} }}
        }}
        if (newLevel !== _batt.level) {{
            _batt.level = Math.round(newLevel * 100) / 100;
            if (!_batt.charging) {{
                _batt.dischargingTime = Math.max(0, Math.floor((_batt.level * 100) / 0.15 * 60));
            }}
            if (_batt.onlevelchange) {{ try {{ _batt.onlevelchange(new Event('levelchange')); }} catch(e2){{}} }}
        }}
    }}, 30000); // update every 30s
    Navigator.prototype.getBattery = function() {{ return Promise.resolve(_batt); }};
    _storeOrig(Navigator.prototype.getBattery, 'getBattery');
}} catch(e) {{}}

// ── navigator.userAgentData (Client Hints JS API) ──
// Chrome version auto-detected from device: {chrome_full}
try {{
    var _uaBrands = Object.freeze([
        Object.freeze({{ brand: 'Chromium', version: '{chrome_major}' }}),
        Object.freeze({{ brand: 'Google Chrome', version: '{chrome_major}' }}),
        Object.freeze({{ brand: 'Not_A Brand', version: '{not_a_brand_ver}' }}),
    ]);
    var _uaFullVersionList = Object.freeze([
        Object.freeze({{ brand: 'Chromium', version: '{chrome_full}' }}),
        Object.freeze({{ brand: 'Google Chrome', version: '{chrome_full}' }}),
        Object.freeze({{ brand: 'Not_A Brand', version: '{not_a_brand_ver}.0.0.0' }}),
    ]);
    var _uaData = Object.create(null);
    var _getUABrands = function() {{ return _uaBrands; }};
    var _getUAMobile = function() {{ return true; }};
    var _getUAPlatform = function() {{ return 'Android'; }};
    _storeOrig(_getUABrands, 'get brands');
    _storeOrig(_getUAMobile, 'get mobile');
    _storeOrig(_getUAPlatform, 'get platform');
    Object.defineProperties(_uaData, {{
        brands: {{ get: _getUABrands, enumerable: true }},
        mobile: {{ get: _getUAMobile, enumerable: true }},
        platform: {{ get: _getUAPlatform, enumerable: true }},
    }});
    _uaData.getHighEntropyValues = function(hints) {{
        return Promise.resolve({{
            brands: _uaBrands,
            mobile: true,
            platform: 'Android',
            platformVersion: {json.dumps(profile.build_version_release)},
            architecture: 'arm',
            bitness: '64',
            model: {json.dumps(profile.model)},
            uaFullVersion: '{chrome_full}',
            fullVersionList: _uaFullVersionList,
        }});
    }};
    _storeOrig(_uaData.getHighEntropyValues, 'getHighEntropyValues');
    _uaData.toJSON = function() {{
        return {{ brands: _uaBrands, mobile: true, platform: 'Android' }};
    }};
    _storeOrig(_uaData.toJSON, 'toJSON');
    // Critical: Set Symbol.toStringTag so toString() returns [object NavigatorUAData]
    Object.defineProperty(_uaData, Symbol.toStringTag, {{
        value: 'NavigatorUAData',
        configurable: true,
        writable: false,
        enumerable: false,
    }});
    // Set prototype to match real NavigatorUAData if available
    try {{
        if (typeof NavigatorUAData !== 'undefined') {{
            Object.setPrototypeOf(_uaData, NavigatorUAData.prototype);
        }}
    }} catch(e) {{}}
    _def(Navigator.prototype, 'userAgentData', _uaData);
}} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 2. SCREEN, DISPLAY & VIEWPORT
// ════════════════════════════════════════════════════════════

var _scr = {{
    width: {profile.screen_width},
    height: {profile.screen_height},
    availWidth: {profile.screen_width},
    availHeight: {avail_height},
    availTop: 0,
    availLeft: 0,
    colorDepth: 24,
    pixelDepth: 24,
}};
for (var _sk in _scr) {{
    _def(Screen.prototype, _sk, _scr[_sk]);
}}

// ── Screen.isExtended (single mobile display) ──
try {{ _def(Screen.prototype, 'isExtended', false); }} catch(e) {{}}

try {{
    _def(window, 'devicePixelRatio', {dpr});
}} catch(e) {{}}

try {{
    var _getOrientType = function() {{ return 'portrait-primary'; }};
    var _getOrientAngle = function() {{ return 0; }};
    _storeOrig(_getOrientType, 'get type');
    _storeOrig(_getOrientAngle, 'get angle');
    Object.defineProperty(ScreenOrientation.prototype, 'type', {{
        get: _getOrientType,
        configurable: false,
    }});
    Object.defineProperty(ScreenOrientation.prototype, 'angle', {{
        get: _getOrientAngle,
        configurable: false,
    }});
}} catch(e) {{}}

// ── Window viewport dimensions (CRITICAL — must match phone, not emulator) ──
// On a real phone, Chrome reports these as CSS pixel viewport dimensions.
// innerWidth = physical_width / DPR
// innerHeight = (physical_height / DPR) - status_bar - nav_bar - chrome_toolbar
var _vpW = {css_viewport_w};
var _vpH = {css_viewport_h};
try {{
    var _getInnerW = function() {{ return _vpW; }};
    var _getInnerH = function() {{ return _vpH; }};
    var _getOuterW = function() {{ return _vpW; }};
    var _getOuterH = function() {{ return _vpH; }};
    _storeOrig(_getInnerW, 'get innerWidth');
    _storeOrig(_getInnerH, 'get innerHeight');
    _storeOrig(_getOuterW, 'get outerWidth');
    _storeOrig(_getOuterH, 'get outerHeight');
    Object.defineProperty(window, 'innerWidth', {{
        get: _getInnerW,
        configurable: false, enumerable: true,
    }});
    Object.defineProperty(window, 'innerHeight', {{
        get: _getInnerH,
        configurable: false, enumerable: true,
    }});
    Object.defineProperty(window, 'outerWidth', {{
        get: _getOuterW,
        configurable: false, enumerable: true,
    }});
    Object.defineProperty(window, 'outerHeight', {{
        get: _getOuterH,
        configurable: false, enumerable: true,
    }});
}} catch(e) {{}}

// ── document.documentElement.clientWidth/clientHeight ──
// On mobile Chrome (overlay scrollbars), these equal innerWidth/innerHeight.
// We override directly on documentElement instance to avoid breaking other elements.
try {{
    // Use MutationObserver to wait for document.documentElement if not ready
    var _getDocCW = function() {{ return _vpW; }};
    var _getDocCH = function() {{ return _vpH; }};
    _storeOrig(_getDocCW, 'get clientWidth');
    _storeOrig(_getDocCH, 'get clientHeight');
    function _patchDocEl() {{
        if (!document.documentElement) return false;
        try {{
            Object.defineProperty(document.documentElement, 'clientWidth', {{
                get: _getDocCW,
                configurable: true,
            }});
            Object.defineProperty(document.documentElement, 'clientHeight', {{
                get: _getDocCH,
                configurable: true,
            }});
        }} catch(e) {{}}
        return true;
    }}
    if (!_patchDocEl()) {{
        // documentElement not ready yet (injected before DOM), use observer
        var _docObs = new MutationObserver(function(m, obs) {{
            if (_patchDocEl()) obs.disconnect();
        }});
        _docObs.observe(document, {{ childList: true }});
    }}
}} catch(e) {{}}

// ── visualViewport API ──
try {{
    if (window.visualViewport) {{
        var _getVVW = function() {{ return _vpW; }};
        var _getVVH = function() {{ return _vpH; }};
        var _getVVScale = function() {{ return 1; }};
        var _getVVZero = function() {{ return 0; }};
        _storeOrig(_getVVW, 'get width');
        _storeOrig(_getVVH, 'get height');
        _storeOrig(_getVVScale, 'get scale');
        _storeOrig(_getVVZero, 'get offsetLeft');
        Object.defineProperty(window.visualViewport, 'width', {{
            get: _getVVW,
            configurable: false, enumerable: true,
        }});
        Object.defineProperty(window.visualViewport, 'height', {{
            get: _getVVH,
            configurable: false, enumerable: true,
        }});
        Object.defineProperty(window.visualViewport, 'scale', {{
            get: _getVVScale,
            configurable: false, enumerable: true,
        }});
        Object.defineProperty(window.visualViewport, 'offsetLeft', {{
            get: _getVVZero,
            configurable: false, enumerable: true,
        }});
        Object.defineProperty(window.visualViewport, 'offsetTop', {{
            get: _getVVZero,
            configurable: false, enumerable: true,
        }});
        Object.defineProperty(window.visualViewport, 'pageLeft', {{
            get: _getVVZero,
            configurable: false, enumerable: true,
        }});
        Object.defineProperty(window.visualViewport, 'pageTop', {{
            get: _getVVZero,
            configurable: false, enumerable: true,
        }});
    }}
}} catch(e) {{}}

// ── matchMedia override ──
// Belt-and-suspenders: Emulation.setDeviceMetricsOverride handles CSS layout,
// but we also wrap matchMedia for additional safety.
try {{
    var _origMatchMedia = window.matchMedia.bind(window);
    window.matchMedia = function(query) {{
        if (typeof query === 'string') {{
            // Rewrite width/height pixel values in media queries
            // e.g., "(max-width: 411px)" — replace with real check against spoofed dims
            var q = query;
            // Replace device-width/device-height with our spoofed screen values
            q = q.replace(/device-width/g, 'width');
            q = q.replace(/device-height/g, 'height');
            // For resolution queries, we can't easily rewrite — let them through
            // Most detection scripts use window.matchMedia('(hover: hover)') or
            // pointer queries, not dimension queries. This is a best-effort patch.
        }}
        return _origMatchMedia(query);
    }};
    // Preserve toString for native code detection
    _storeOrig(window.matchMedia, 'matchMedia');
}} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 3. WEBGL — Renderer/Vendor + ALL Numeric Parameters
// ════════════════════════════════════════════════════════════

var _glR = {json.dumps(profile.gl_renderer)};
var _glV = {json.dumps(profile.gl_vendor)};

// GPU capability database — accurate per-GPU values as seen in Chrome/ANGLE
var _gpuParams = {gpu_params_js};

// WebGL constant → GPU param key mapping
var _glParamMap = {{
    0x0D33: 'MAX_TEXTURE_SIZE',
    0x851C: 'MAX_CUBE_MAP_TEXTURE_SIZE',
    0x84E8: 'MAX_RENDERBUFFER_SIZE',
    0x0D3A: 'MAX_VIEWPORT_DIMS',
    0x8073: 'MAX_3D_TEXTURE_SIZE',
    0x88FF: 'MAX_ARRAY_TEXTURE_LAYERS',
    0x846D: 'ALIASED_POINT_SIZE_RANGE',
    0x846E: 'ALIASED_LINE_WIDTH_RANGE',
    0x8872: 'MAX_TEXTURE_IMAGE_UNITS',
    0x8B4D: 'MAX_COMBINED_TEXTURE_IMAGE_UNITS',
    0x8B4C: 'MAX_VERTEX_TEXTURE_IMAGE_UNITS',
    0x8DFD: 'MAX_FRAGMENT_UNIFORM_VECTORS',
    0x8DFE: 'MAX_VERTEX_UNIFORM_VECTORS',
    0x8869: 'MAX_VERTEX_ATTRIBS',
    0x8B49: 'MAX_VERTEX_UNIFORM_COMPONENTS',
    0x8A2B: 'MAX_VERTEX_UNIFORM_BLOCKS',
    0x9122: 'MAX_VERTEX_OUTPUT_COMPONENTS',
    0x8DFC: 'MAX_VARYING_VECTORS',
    0x8B4B: 'MAX_VARYING_COMPONENTS',
    0x8B4A: 'MAX_FRAGMENT_UNIFORM_COMPONENTS',
    0x8A2D: 'MAX_FRAGMENT_UNIFORM_BLOCKS',
    0x9125: 'MAX_FRAGMENT_INPUT_COMPONENTS',
    0x8824: 'MAX_DRAW_BUFFERS',
    0x8CDF: 'MAX_COLOR_ATTACHMENTS',
    0x8D57: 'MAX_SAMPLES',
    0x8A2F: 'MAX_UNIFORM_BUFFER_BINDINGS',
    0x8A30: 'MAX_UNIFORM_BLOCK_SIZE',
    0x8A31: 'UNIFORM_BUFFER_OFFSET_ALIGNMENT',
    0x8A2E: 'MAX_COMBINED_UNIFORM_BLOCKS',
    0x8A45: 'MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS',
    0x8A46: 'MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS',
    0x8C8A: 'MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS',
    0x8C8B: 'MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS',
    0x8C80: 'MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS',
    0x8904: 'MIN_PROGRAM_TEXEL_OFFSET',
    0x8905: 'MAX_PROGRAM_TEXEL_OFFSET',
    0x84FD: 'MAX_TEXTURE_LOD_BIAS',
}};

function _wrapGL(proto) {{
    if (!proto || !proto.getParameter) return;
    var _orig = proto.getParameter;
    proto.getParameter = function(p) {{
        // Renderer/Vendor string overrides
        if (p === 0x9246) return _glR;  // UNMASKED_RENDERER_WEBGL
        if (p === 0x9245) return _glV;  // UNMASKED_VENDOR_WEBGL
        if (p === 0x1F01) return _glR;  // GL_RENDERER
        if (p === 0x1F00) return _glV;  // GL_VENDOR
        // Numeric GPU capability overrides
        var _key = _glParamMap[p];
        if (_key && _gpuParams.hasOwnProperty(_key)) {{
            var _val = _gpuParams[_key];
            if (Array.isArray(_val)) {{
                // Return Float32Array for range params, Int32Array for dims
                if (_key.indexOf('RANGE') >= 0 || _key.indexOf('WIDTH') >= 0) {{
                    return new Float32Array(_val);
                }}
                return new Int32Array(_val);
            }}
            return _val;
        }}
        return _orig.call(this, p);
    }};
    _storeOrig(proto.getParameter, 'getParameter');
}}
try {{ _wrapGL(WebGLRenderingContext.prototype); }} catch(e) {{}}
try {{ _wrapGL(WebGL2RenderingContext.prototype); }} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 3b. WEBGL SHADER COMPILATION TIMING NORMALIZATION
// ════════════════════════════════════════════════════════════

// Shader compilation timing reveals the underlying GPU/driver: a real Adreno 730
// compiles a trivial shader in ~0.15ms with a 15x ratio for complex shaders.
// An emulator (ANGLE→HLSL/SPIRV→desktop GPU) shows a flatter ~3x ratio because
// the ANGLE translation overhead is roughly constant. We intercept the timing-
// sensitive APIs and inject calibrated delays to match the target mobile GPU.

try {{
var _shT = {{
    base: {shader_timing['base_compile_ms']},      // base compile ms (trivial shader)
    link: {shader_timing['base_link_ms']},          // base link ms (trivial shader)
    scale: {shader_timing['complexity_scale']},     // max complexity multiplier
    lc: {shader_timing['link_to_compile']},         // link/compile ratio
    jit: {shader_timing['jitter_pct']},             // jitter ±%
}};

// Shader source tracking (WeakMap for GC safety)
var _shSrc = new WeakMap();   // shader → GLSL source string
var _shTime = new WeakMap();  // shader → {{ start: ms, complexity: float }}
var _pgTime = new WeakMap();  // program → {{ start: ms, complexity: float }}
var _pgShaders = new WeakMap(); // program → [shader, ...]

// Deterministic jitter using our PRNG
function _shJit(base) {{
    var j = ((_rng() / 0xFFFFFFFF) - 0.5) * 2 * _shT.jit;
    return base * (1.0 + j);
}}

// Estimate GLSL complexity by tokenizing the source
function _shComplex(src) {{
    if (!src || src.length < 20) return 1.0;
    var score = 1.0;
    // Loops (most expensive: each iteration = more instructions to compile)
    var loops = src.match(/\\bfor\\s*\\(/g);
    if (loops) score += loops.length * 3.0;
    var whiles = src.match(/\\bwhile\\s*\\(/g);
    if (whiles) score += whiles.length * 2.5;
    // Texture lookups
    var tex = src.match(/texture2D|textureCube|texture\\s*\\(/g);
    if (tex) score += tex.length * 1.5;
    // Trig / math functions (require ALU micro-ops)
    var math = src.match(/\\b(sin|cos|tan|pow|exp|exp2|log|log2|sqrt|inversesqrt|abs|sign|floor|ceil|fract|mod|clamp|mix|smoothstep|step)\\b/g);
    if (math) score += math.length * 0.3;
    // Conditionals (branching = divergence cost)
    var ifs = src.match(/\\bif\\s*\\(/g);
    if (ifs) score += ifs.length * 0.5;
    // Uniform / varying declarations
    var unis = src.match(/\\buniform\\s+/g);
    if (unis) score += unis.length * 0.2;
    // Source length as rough proxy (more GLSL = more parsing)
    score += (src.length / 500) * 0.5;
    // Cap at the GPU's complexity scale
    return Math.min(score, _shT.scale);
}}

// Busy-wait for precise sub-ms delays (setTimeout is 4ms minimum)
function _shWait(ms) {{
    if (ms <= 0) return;
    var target = _origPerfNow.call(performance) + ms;
    while (_origPerfNow.call(performance) < target) {{}}
}}

// ── Hook shaderSource: capture GLSL for complexity estimation ──
function _wrapShaderTiming(proto) {{
    if (!proto) return;

    var _oSS = proto.shaderSource;
    if (_oSS) {{
        proto.shaderSource = function(sh, src) {{
            _shSrc.set(sh, src);
            return _oSS.call(this, sh, src);
        }};
        _storeOrig(proto.shaderSource, 'shaderSource');
    }}

    // ── Hook compileShader: record start time ──
    var _oCS = proto.compileShader;
    if (_oCS) {{
        proto.compileShader = function(sh) {{
            var src = _shSrc.get(sh) || '';
            _shTime.set(sh, {{
                start: _origPerfNow.call(performance),
                complexity: _shComplex(src),
            }});
            return _oCS.call(this, sh);
        }};
        _storeOrig(proto.compileShader, 'compileShader');
    }}

    // ── Hook getShaderParameter: enforce minimum compile duration ──
    var _oGSP = proto.getShaderParameter;
    if (_oGSP) {{
        proto.getShaderParameter = function(sh, pname) {{
            var result = _oGSP.call(this, sh, pname);
            // COMPILE_STATUS (0x8B81): synchronous — busy-wait until target time
            if (pname === 0x8B81) {{
                var info = _shTime.get(sh);
                if (info) {{
                    var elapsed = _origPerfNow.call(performance) - info.start;
                    var target = _shJit(_shT.base * info.complexity);
                    if (elapsed < target) _shWait(target - elapsed);
                    _shTime.delete(sh);
                }}
            }}
            // COMPLETION_STATUS_KHR (0x91B1): async poll — return false until target
            if (pname === 0x91B1) {{
                var info2 = _shTime.get(sh);
                if (info2) {{
                    var elapsed2 = _origPerfNow.call(performance) - info2.start;
                    var target2 = _shJit(_shT.base * info2.complexity);
                    if (elapsed2 < target2) return false;
                    _shTime.delete(sh);
                }}
            }}
            return result;
        }};
        _storeOrig(proto.getShaderParameter, 'getShaderParameter');
    }}

    // ── Hook attachShader: track program's shaders for aggregate complexity ──
    var _oAS = proto.attachShader;
    if (_oAS) {{
        proto.attachShader = function(prog, sh) {{
            var list = _pgShaders.get(prog);
            if (!list) {{ list = []; _pgShaders.set(prog, list); }}
            list.push(sh);
            return _oAS.call(this, prog, sh);
        }};
        _storeOrig(proto.attachShader, 'attachShader');
    }}

    // ── Hook linkProgram: record start time with aggregate complexity ──
    var _oLP = proto.linkProgram;
    if (_oLP) {{
        proto.linkProgram = function(prog) {{
            // Aggregate complexity from all attached shaders
            var shaders = _pgShaders.get(prog) || [];
            var maxComplexity = 1.0;
            for (var i = 0; i < shaders.length; i++) {{
                var src = _shSrc.get(shaders[i]) || '';
                maxComplexity = Math.max(maxComplexity, _shComplex(src));
            }}
            _pgTime.set(prog, {{
                start: _origPerfNow.call(performance),
                complexity: maxComplexity,
            }});
            return _oLP.call(this, prog);
        }};
        _storeOrig(proto.linkProgram, 'linkProgram');
    }}

    // ── Hook getProgramParameter: enforce minimum link duration ──
    var _oGPP = proto.getProgramParameter;
    if (_oGPP) {{
        proto.getProgramParameter = function(prog, pname) {{
            var result = _oGPP.call(this, prog, pname);
            // LINK_STATUS (0x8B82): synchronous
            if (pname === 0x8B82) {{
                var info = _pgTime.get(prog);
                if (info) {{
                    var elapsed = _origPerfNow.call(performance) - info.start;
                    var target = _shJit(_shT.link * info.complexity * _shT.lc);
                    if (elapsed < target) _shWait(target - elapsed);
                    _pgTime.delete(prog);
                }}
            }}
            // COMPLETION_STATUS_KHR (0x91B1): async poll
            if (pname === 0x91B1) {{
                var info2 = _pgTime.get(prog);
                if (info2) {{
                    var elapsed2 = _origPerfNow.call(performance) - info2.start;
                    var target2 = _shJit(_shT.link * info2.complexity * _shT.lc);
                    if (elapsed2 < target2) return false;
                    _pgTime.delete(prog);
                }}
            }}
            return result;
        }};
        _storeOrig(proto.getProgramParameter, 'getProgramParameter');
    }}

    // ── Hook getShaderInfoLog: also blocks, must not return before compile target ──
    var _oGSIL = proto.getShaderInfoLog;
    if (_oGSIL) {{
        proto.getShaderInfoLog = function(sh) {{
            var info = _shTime.get(sh);
            if (info) {{
                var elapsed = _origPerfNow.call(performance) - info.start;
                var target = _shJit(_shT.base * info.complexity);
                if (elapsed < target) _shWait(target - elapsed);
                _shTime.delete(sh);
            }}
            return _oGSIL.call(this, sh);
        }};
        _storeOrig(proto.getShaderInfoLog, 'getShaderInfoLog');
    }}

    // ── Hook getProgramInfoLog: also blocks ──
    var _oGPIL = proto.getProgramInfoLog;
    if (_oGPIL) {{
        proto.getProgramInfoLog = function(prog) {{
            var info = _pgTime.get(prog);
            if (info) {{
                var elapsed = _origPerfNow.call(performance) - info.start;
                var target = _shJit(_shT.link * info.complexity * _shT.lc);
                if (elapsed < target) _shWait(target - elapsed);
                _pgTime.delete(prog);
            }}
            return _oGPIL.call(this, prog);
        }};
        _storeOrig(proto.getProgramInfoLog, 'getProgramInfoLog');
    }}
}}

_wrapShaderTiming(WebGLRenderingContext.prototype);
try {{ _wrapShaderTiming(WebGL2RenderingContext.prototype); }} catch(e) {{}}
}} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 4. CANVAS FINGERPRINT NOISE
// ════════════════════════════════════════════════════════════

try {{
    var _origToDU = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function() {{
        try {{
            var ctx = this.getContext && this.getContext('2d');
            if (ctx && this.width > 0 && this.height > 0 && this.width < 4000) {{
                var img = ctx.getImageData(0, 0, Math.min(this.width, 64), Math.min(this.height, 64));
                var d = img.data;
                for (var i = 0; i < d.length; i += 4) {{
                    d[i] = (d[i] + (_rng() & 1)) & 0xFF;
                }}
                ctx.putImageData(img, 0, 0);
            }}
        }} catch(ex) {{}}
        return _origToDU.apply(this, arguments);
    }};
    _storeOrig(HTMLCanvasElement.prototype.toDataURL, 'toDataURL');
}} catch(e) {{}}

try {{
    var _origGID = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function() {{
        var data = _origGID.apply(this, arguments);
        try {{
            var d = data.data;
            for (var i = 0; i < d.length; i += 4) {{
                d[i] = (d[i] + (_rng() & 1)) & 0xFF;
            }}
        }} catch(ex) {{}}
        return data;
    }};
    _storeOrig(CanvasRenderingContext2D.prototype.getImageData, 'getImageData');
}} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 5. AUDIO FINGERPRINT NOISE
// ════════════════════════════════════════════════════════════

try {{
    var _origSR = OfflineAudioContext.prototype.startRendering;
    OfflineAudioContext.prototype.startRendering = function() {{
        return _origSR.call(this).then(function(buf) {{
            try {{
                var ch = buf.getChannelData(0);
                for (var i = 0; i < ch.length; i += 100) {{
                    ch[i] += _noiseF();
                }}
            }} catch(ex) {{}}
            return buf;
        }});
    }};
    _storeOrig(OfflineAudioContext.prototype.startRendering, 'startRendering');
}} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 6. SPEECHSYNTHESIS — Realistic mobile voice list
// ════════════════════════════════════════════════════════════

// Chrome on Android ships a minimal TTS voice list (just Google voices).
// An emulator often has NO voices or desktop voices. We override getVoices()
// to return a realistic Android Chrome voice set.
try {{
    var _fakeVoices = null;
    function _buildVoices() {{
        if (_fakeVoices) return _fakeVoices;
        // Typical Chrome on Android voices (Google TTS engine)
        var _voiceDefs = [
            ['Google US English', 'en-US', true],
            ['Google UK English Female', 'en-GB', false],
            ['Google UK English Male', 'en-GB', false],
            ['Google español', 'es-ES', false],
            ['Google français', 'fr-FR', false],
            ['Google Deutsch', 'de-DE', false],
            ['Google italiano', 'it-IT', false],
            ['Google português do Brasil', 'pt-BR', false],
            ['Google 日本語', 'ja-JP', false],
            ['Google 한국의', 'ko-KR', false],
            ['Google 中文（普通话）', 'zh-CN', false],
            ['Google हिन्दी', 'hi-IN', false],
        ];
        _fakeVoices = _voiceDefs.map(function(d) {{
            return Object.freeze({{
                name: d[0],
                lang: d[1],
                default: d[2],
                localService: true,
                voiceURI: d[0],
            }});
        }});
        Object.freeze(_fakeVoices);
        return _fakeVoices;
    }}
    var _origGetVoices = SpeechSynthesis.prototype.getVoices;
    SpeechSynthesis.prototype.getVoices = function() {{
        return _buildVoices();
    }};
    _storeOrig(SpeechSynthesis.prototype.getVoices, 'getVoices');
    // Fire voiceschanged event after a short delay (mimics real behavior)
    setTimeout(function() {{
        try {{
            if (window.speechSynthesis && window.speechSynthesis.onvoiceschanged) {{
                window.speechSynthesis.onvoiceschanged(new Event('voiceschanged'));
            }}
            window.speechSynthesis.dispatchEvent(new Event('voiceschanged'));
        }} catch(e) {{}}
    }}, 50);
}} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 7. TIMING SIDE-CHANNEL MITIGATION
// ════════════════════════════════════════════════════════════

// Reduce precision of performance.now() to 100μs (like cross-origin-isolated)
// This prevents micro-timing attacks that could fingerprint the CPU/emulator.
// Note: _origPerfNow was captured at the top of the payload (before Section 1)
// so that both shader timing (Section 3b) and this section can use the real timer.
try {{
    Performance.prototype.now = function() {{
        return Math.round(_origPerfNow.call(this) * 10) / 10;
    }};
    _storeOrig(Performance.prototype.now, 'now');
}} catch(e) {{}}

// Clamp Date.now() to millisecond (remove sub-ms if any) and add micro-jitter
try {{
    var _origDateNow = Date.now;
    Date.now = function() {{
        return _origDateNow.call(Date);
    }};
    _storeOrig(Date.now, 'now');
}} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 8. HARDENING — WebRTC + prototype detection
// ════════════════════════════════════════════════════════════
// NOTE: Function.prototype.toString hardening has been MOVED to the top
// of the payload (right after _storeOrig declaration) so it is active
// BEFORE any getters are created. See the block after _storeOrig above.

// Block WebRTC local IP leak
try {{
    var _origRTC = window.RTCPeerConnection;
    if (_origRTC) {{
        window.RTCPeerConnection = function(config) {{
            if (!config) config = {{}};
            config.iceServers = [];
            return new _origRTC(config);
        }};
        window.RTCPeerConnection.prototype = _origRTC.prototype;
    }}
}} catch(e) {{}}

// ════════════════════════════════════════════════════════════
// 9. ADDITIONAL SPOOFS — Gaps that could leak emulator identity
// ════════════════════════════════════════════════════════════

// ── MediaDevices.enumerateDevices() ──
// Real phones: front/rear camera + earpiece + speaker
// Emulators: desktop devices (webcam, microphone, speaker)
try {{
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
        var _origEnumDev = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
        var _fakeDevices = [
            {{ deviceId: 'default', kind: 'audioinput', label: 'Bottom Microphone', groupId: 'mic0' }},
            {{ deviceId: 'cam0', kind: 'videoinput', label: 'camera2 0, facing back', groupId: 'cam0' }},
            {{ deviceId: 'cam1', kind: 'videoinput', label: 'camera2 1, facing front', groupId: 'cam1' }},
            {{ deviceId: 'default', kind: 'audiooutput', label: 'Earpiece', groupId: 'spk0' }},
            {{ deviceId: 'spk1', kind: 'audiooutput', label: 'Speaker', groupId: 'spk1' }},
        ];
        // Wrap each fake device so it looks like a real MediaDeviceInfo
        _fakeDevices = _fakeDevices.map(function(d) {{
            var obj = Object.create(MediaDeviceInfo.prototype);
            Object.defineProperties(obj, {{
                deviceId: {{ get: function() {{ return d.deviceId; }}, enumerable: true }},
                kind: {{ get: function() {{ return d.kind; }}, enumerable: true }},
                label: {{ get: function() {{ return d.label; }}, enumerable: true }},
                groupId: {{ get: function() {{ return d.groupId; }}, enumerable: true }},
                toJSON: {{ value: function() {{ return d; }} }},
            }});
            return obj;
        }});
        navigator.mediaDevices.enumerateDevices = function() {{
            return Promise.resolve(_fakeDevices);
        }};
        _storeOrig(navigator.mediaDevices.enumerateDevices, 'enumerateDevices');
    }}
}} catch(e) {{}}

// ── navigator.permissions.query() ──
// Ensure permission states match a real phone (geolocation=granted, etc.)
try {{
    if (navigator.permissions && navigator.permissions.query) {{
        var _origPermQuery = navigator.permissions.query.bind(navigator.permissions);
        var _permMap = {{
            'geolocation': 'granted',
            'notifications': 'denied',
            'camera': 'prompt',
            'microphone': 'prompt',
            'accelerometer': 'granted',
            'gyroscope': 'granted',
            'magnetometer': 'granted',
            'midi': 'prompt',
            'clipboard-read': 'prompt',
            'clipboard-write': 'granted',
        }};
        navigator.permissions.query = function(desc) {{
            var name = desc && desc.name;
            if (name && _permMap[name] !== undefined) {{
                return Promise.resolve({{ state: _permMap[name], onchange: null,
                    addEventListener: function(){{}}, removeEventListener: function(){{}},
                    dispatchEvent: function(){{ return true; }} }});
            }}
            return _origPermQuery(desc);
        }};
        _storeOrig(navigator.permissions.query, 'query');
    }}
}} catch(e) {{}}

// ── navigator.storage.estimate() ──
// Real phones: 4-32 GB available. Emulators: often tiny or desktop-sized
try {{
    if (navigator.storage && navigator.storage.estimate) {{
        var _origStorEst = navigator.storage.estimate.bind(navigator.storage);
        navigator.storage.estimate = function() {{
            var totalGB = {profile.ram_gb} * 4; // rough correlation: 4x RAM for storage
            var usedGB = 1.2 + (_rng() % 50) / 10; // 1.2 - 6.2 GB used
            return Promise.resolve({{
                quota: totalGB * 1024 * 1024 * 1024,
                usage: Math.floor(usedGB * 1024 * 1024 * 1024),
                usageDetails: {{}},
            }});
        }};
        _storeOrig(navigator.storage.estimate, 'estimate');
    }}
}} catch(e) {{}}

// ── WebGL getSupportedExtensions() ──
// Filter out desktop-only extensions that don't appear on real Android phones
try {{
    var _desktopOnlyExts = [
        'WEBGL_debug_renderer_info',     // Chrome disables this on Android
        'WEBGL_debug_shaders',
        'EXT_disjoint_timer_query',      // Disabled on Android for Spectre
        'EXT_disjoint_timer_query_webgl2',
    ];
    // Mobile-only extensions to add if missing
    var _mobileExts = [
        'EXT_color_buffer_half_float',
        'OES_texture_half_float_linear',
        'WEBGL_compressed_texture_astc',
        'WEBGL_compressed_texture_etc',
        'EXT_texture_filter_anisotropic',
    ];

    function _patchGLExtensions(proto) {{
        var _origGetExts = proto.getSupportedExtensions;
        if (!_origGetExts) return;
        proto.getSupportedExtensions = function() {{
            var exts = _origGetExts.call(this);
            if (!exts) return exts;
            // Remove desktop-only extensions
            exts = exts.filter(function(e) {{ return _desktopOnlyExts.indexOf(e) === -1; }});
            // Add mobile extensions if missing
            for (var i = 0; i < _mobileExts.length; i++) {{
                if (exts.indexOf(_mobileExts[i]) === -1) {{
                    exts.push(_mobileExts[i]);
                }}
            }}
            return exts;
        }};
    }}
    if (window.WebGLRenderingContext) _patchGLExtensions(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) _patchGLExtensions(WebGL2RenderingContext.prototype);
}} catch(e) {{}}

// ── navigator.bluetooth / navigator.usb ──
// Block desktop-only Web APIs that don't exist on Android Chrome
try {{ if (navigator.bluetooth) _def(Navigator.prototype, 'bluetooth', undefined); }} catch(e) {{}}
try {{ if (navigator.usb) _def(Navigator.prototype, 'usb', undefined); }} catch(e) {{}}

// ── navigator.getGamepads() ──
// Android phones rarely expose gamepads; return empty array
try {{
    if (Navigator.prototype.getGamepads) {{
        Navigator.prototype.getGamepads = function() {{ return []; }};
        _storeOrig(Navigator.prototype.getGamepads, 'getGamepads');
    }}
}} catch(e) {{}}

// ── navigator.vibrate() ──
// Real phones support vibration; emulators often don't. Always return true.
try {{
    Navigator.prototype.vibrate = function() {{ return true; }};
    _storeOrig(Navigator.prototype.vibrate, 'vibrate');
}} catch(e) {{}}

}})();"""


# ─── CDP Client ─────────────────────────────────────────────────────────────


class CDPClient:
    """Synchronous WebSocket connection to a single Chrome DevTools target."""

    def __init__(self, ws_url: str):
        self._ws_url = ws_url
        self._ws = None
        self._msg_id = 0
        self._lock = threading.Lock()
        self._connected = False

    def connect(self, timeout: float = 5.0) -> bool:
        """Establish WebSocket connection."""
        if websocket is None:
            return False
        try:
            self._ws = websocket.create_connection(
                self._ws_url,
                timeout=timeout,
                suppress_origin=True,
            )
            self._connected = True
            return True
        except Exception:
            self._connected = False
            return False

    def disconnect(self):
        """Close WebSocket connection."""
        self._connected = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None

    def send_command(self, method: str, params: Optional[dict] = None, timeout: float = 10.0) -> dict:
        """Send a CDP JSON-RPC command and wait for the matching response."""
        if not self._connected or not self._ws:
            return {"error": {"message": "Not connected"}}
        with self._lock:
            self._msg_id += 1
            msg_id = self._msg_id
        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        try:
            self._ws.send(json.dumps(msg))
            # Wait for matching response (skip events)
            deadline = time.time() + timeout
            while time.time() < deadline:
                self._ws.settimeout(max(0.5, deadline - time.time()))
                raw = self._ws.recv()
                if not raw:
                    continue
                resp = json.loads(raw)
                if resp.get("id") == msg_id:
                    return resp
            return {"error": {"message": "Timeout"}}
        except Exception as e:
            self._connected = False
            return {"error": {"message": str(e)}}

    def inject_script(self, source: str) -> str:
        """Call Page.addScriptToEvaluateOnNewDocument. Returns identifier."""
        resp = self.send_command("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        return resp.get("result", {}).get("identifier", "")

    def remove_script(self, identifier: str):
        """Remove a previously injected script."""
        if identifier:
            self.send_command("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})

    def set_user_agent_override(self, profile: DeviceProfile):
        """Override User-Agent + Client Hints via Network.setUserAgentOverride."""
        android_ver = profile.build_version_release or "13"
        chrome_full = profile.chrome_version or "133.0.6943.137"
        chrome_major = profile.chrome_major or "133"
        not_a_brand_ver = _not_a_brand_version(chrome_major)
        ua_metadata = {
            "brands": [
                {"brand": "Chromium", "version": chrome_major},
                {"brand": "Google Chrome", "version": chrome_major},
                {"brand": "Not_A Brand", "version": not_a_brand_ver},
            ],
            "fullVersionList": [
                {"brand": "Chromium", "version": chrome_full},
                {"brand": "Google Chrome", "version": chrome_full},
                {"brand": "Not_A Brand", "version": f"{not_a_brand_ver}.0.0.0"},
            ],
            "fullVersion": chrome_full,
            "platform": "Android",
            "platformVersion": android_ver,
            "architecture": "arm",
            "bitness": "64",
            "model": profile.model or "Pixel 7",
            "mobile": True,
            "wow64": False,
        }
        self.send_command("Network.setUserAgentOverride", {
            "userAgent": profile.user_agent,
            "platform": profile.platform,
            "userAgentMetadata": ua_metadata,
        })

    def set_geolocation_override(self, latitude: float, longitude: float, accuracy: float):
        """Override geolocation via Emulation.setGeolocationOverride.

        This makes navigator.geolocation.getCurrentPosition() and watchPosition()
        return the spoofed coordinates instead of querying the real device location.
        CDP handles this at the browser engine level — no JS override needed.
        """
        # First, grant geolocation permission so the browser doesn't show a prompt
        try:
            self.send_command("Browser.grantPermissions", {
                "permissions": ["geolocation"],
            })
        except Exception:
            # Browser.grantPermissions may not be available on older Chrome
            pass

        self.send_command("Emulation.setGeolocationOverride", {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy": accuracy,
        })

    def clear_geolocation_override(self):
        """Remove geolocation override (revert to real location)."""
        self.send_command("Emulation.clearGeolocationOverride")

    def set_device_metrics_override(self, profile: DeviceProfile):
        """Override device metrics via Emulation.setDeviceMetricsOverride.

        This tells Chrome to render as if the physical screen is a different
        size.  Without this, window.innerWidth/Height, div.clientWidth/Height,
        and CSS media queries still reflect the real emulator window — a dead
        giveaway that the spoofed 1440×3216 screen.* values don't match the
        actual 894×540 viewport.

        CDP handles this at the compositor level: Chrome genuinely re-layouts
        the page into the overridden dimensions, so every DOM measurement
        (getBoundingClientRect, offsetWidth, scrollWidth, matchMedia, etc.)
        returns values consistent with the spoofed screen.
        """
        phys_w = int(profile.screen_width) if profile.screen_width else 1440
        phys_h = int(profile.screen_height) if profile.screen_height else 3200
        density = int(profile.screen_density) if profile.screen_density else 420

        # Compute DPR from density (same logic as generate_spoof_payload)
        dpr = _dpr_for_device(profile.model, phys_w, density)

        # width/height params are CSS pixels; screenWidth/screenHeight are physical.
        # Chrome re-layouts the page into these CSS dimensions, so
        # div.clientWidth, innerWidth, matchMedia() etc. all match the
        # spoofed screen size exactly.
        #
        # CRITICAL: height must subtract browser chrome (status bar + nav bar + toolbar)
        # to match what window.innerHeight reports on a real phone.
        # Without this, a position:fixed div would measure full CSS height while
        # window.innerHeight reports a smaller value — a detectable mismatch.
        status_bar_dp = 24    # Android status bar
        nav_bar_dp = 48       # 3-button navigation bar
        chrome_toolbar_dp = 56  # Chrome omnibox/toolbar
        css_w = round(phys_w / dpr)
        css_h = round(phys_h / dpr) - status_bar_dp - nav_bar_dp - chrome_toolbar_dp

        self.send_command("Emulation.setDeviceMetricsOverride", {
            "width": css_w,
            "height": css_h,
            "deviceScaleFactor": dpr,
            "mobile": True,
            "screenWidth": phys_w,
            "screenHeight": phys_h,
            "screenOrientation": {"type": "portraitPrimary", "angle": 0},
        })

    def clear_device_metrics_override(self):
        """Remove device metrics override (revert to real viewport)."""
        self.send_command("Emulation.clearDeviceMetricsOverride")

    def set_timezone_override(self, timezone_id: str):
        """Override browser timezone via Emulation.setTimezoneOverride.

        This makes JavaScript's Intl.DateTimeFormat().resolvedOptions().timeZone,
        new Date().getTimezoneOffset(), and toLocaleString() all return values
        consistent with the specified IANA timezone.

        Critical for Jagex: if the system property says America/New_York but
        Chrome's JS returns Europe/London, that's a cross-layer mismatch detection.
        """
        self.send_command("Emulation.setTimezoneOverride", {
            "timezoneId": timezone_id,
        })

    def enable_domains(self):
        """Enable ONLY minimum required CDP domains.

        Page.enable  — required for addScriptToEvaluateOnNewDocument + Page.reload
        Network.enable — REMOVED: it creates detectable side effects
            (modifies performance.getEntries(), PerformanceObserver behavior).
            Network.setUserAgentOverride works WITHOUT Network.enable.
        Emulation — has no explicit enable method; its commands work immediately.
        """
        self.send_command("Page.enable")
        # DO NOT enable Network — it's a major detection vector.
        # Network.setUserAgentOverride works without it.

    def reload_page(self):
        """Force page reload so injected scripts take effect."""
        self.send_command("Page.reload")

    def cleanup_cdp_artifacts(self):
        """Execute immediate cleanup of CDP artifacts on the CURRENT page.

        Page.addScriptToEvaluateOnNewDocument only fires on the NEXT document.
        This method cleans up the current page via Runtime.evaluate so that
        cdc_* properties and navigator.webdriver are fixed RIGHT NOW.
        """
        cleanup_js = """(function() {
            // Remove cdc_ and automation properties from window
            try {
                Object.keys(window).forEach(function(k) {
                    if (/^cdc_|^__webdriver|^__selenium|^__driver|^\\$cdc_/.test(k)) {
                        try { delete window[k]; } catch(e) {}
                    }
                });
            } catch(e) {}
            // Remove from document
            try {
                Object.keys(document).forEach(function(k) {
                    if (/^cdc_|^__webdriver|^\\$cdc_/.test(k)) {
                        try { delete document[k]; } catch(e) {}
                    }
                });
            } catch(e) {}
            // Fix navigator.webdriver on current page
            try {
                delete Navigator.prototype.webdriver;
                Object.defineProperty(Navigator.prototype, 'webdriver', {
                    get: function() { return false; },
                    configurable: true,
                    enumerable: true,
                });
            } catch(e) {}
            // Fix Notification.permission
            try {
                Object.defineProperty(Notification, 'permission', {
                    get: function() { return 'denied'; },
                    configurable: true,
                });
            } catch(e) {}
        })();"""
        try:
            self.send_command("Runtime.evaluate", {
                "expression": cleanup_js,
                "returnByValue": True,
            })
        except Exception:
            pass  # Non-fatal: page may not be ready yet

    @property
    def connected(self) -> bool:
        return self._connected


# ─── CDP Manager ─────────────────────────────────────────────────────────────


@dataclass
class InstanceCDPState:
    """Tracks CDP injection state for a single BlueStacks instance."""
    instance_name: str
    adb_serial: str
    local_port: int
    enabled: bool = False
    connected: bool = False
    status: str = "idle"  # idle | polling | connecting | injected | error
    error_msg: str = ""
    profile: Optional[DeviceProfile] = None
    client: Optional[CDPClient] = None
    injected_id: str = ""
    poll_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)


class CDPManager:
    """Manages CDP browser fingerprint injection across all BlueStacks instances."""

    def __init__(self, adb_exe: str, on_status_change: Optional[Callable] = None):
        self._adb_exe = adb_exe
        self._instances: dict[str, InstanceCDPState] = {}
        self._on_status_change = on_status_change
        self._lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────

    def enable_instance(self, instance_name: str, adb_serial: str, local_port: int):
        """Start CDP injection monitoring for an instance."""
        if websocket is None:
            self._fire_status(instance_name, "error", "websocket-client not installed. Run: pip install websocket-client")
            return

        with self._lock:
            if instance_name in self._instances:
                old = self._instances[instance_name]
                if old.enabled:
                    return  # Already running
                old.stop_event.set()

            state = InstanceCDPState(
                instance_name=instance_name,
                adb_serial=adb_serial,
                local_port=local_port,
                enabled=True,
            )
            self._instances[instance_name] = state

        t = threading.Thread(target=self._poll_loop, args=(state,), daemon=True)
        state.poll_thread = t
        t.start()

    def disable_instance(self, instance_name: str):
        """Stop CDP injection for an instance."""
        with self._lock:
            state = self._instances.pop(instance_name, None)
        if state:
            state.enabled = False
            state.stop_event.set()
            if state.client:
                state.client.disconnect()
            self._remove_forward(state)
            self._fire_status(instance_name, "idle", "")

    def disable_all(self):
        """Stop CDP for all instances."""
        with self._lock:
            names = list(self._instances.keys())
        for name in names:
            self.disable_instance(name)

    def get_status(self, instance_name: str) -> dict:
        """Get current CDP status for an instance."""
        with self._lock:
            state = self._instances.get(instance_name)
        if not state:
            return {"enabled": False, "connected": False, "status": "idle", "error": ""}
        return {
            "enabled": state.enabled,
            "connected": state.connected,
            "status": state.status,
            "error": state.error_msg,
        }

    def get_all_statuses(self) -> dict:
        """Get CDP status dict for all tracked instances."""
        with self._lock:
            names = list(self._instances.keys())
        out = {}
        for name in names:
            out[name] = self.get_status(name)
        return out

    def reload_profile(self, instance_name: str):
        """Re-read profile and re-inject for an instance (call after randomize)."""
        with self._lock:
            state = self._instances.get(instance_name)
        if not state or not state.enabled:
            return

        try:
            profile = read_device_profile(self._adb_exe, state.adb_serial)
            state.profile = profile

            if state.client and state.client.connected:
                # Remove old injection
                if state.injected_id:
                    state.client.remove_script(state.injected_id)

                # Re-inject with new profile
                payload = generate_spoof_payload(profile)
                new_id = state.client.inject_script(payload)
                state.injected_id = new_id
                state.client.set_user_agent_override(profile)

                # Re-apply device metrics override with new profile dimensions
                try:
                    state.client.set_device_metrics_override(profile)
                except Exception:
                    pass

                # Re-apply geolocation override with new profile's country
                try:
                    country = (profile.carrier_country or "us").upper()
                    seed = _seed_from_fingerprint(profile.build_fingerprint)
                    gps = gps_for_country(country, seed)
                    state.client.set_geolocation_override(gps["lat"], gps["lon"], gps["acc"])
                except Exception:
                    pass

                state.client.reload_page()
                # Post-reload cleanup for cdc_* race condition
                time.sleep(0.5)
                state.client.cleanup_cdp_artifacts()
                self._update_state(state, "injected", "")
        except Exception as e:
            self._update_state(state, "error", str(e))

    # ── Internal ─────────────────────────────────────────────

    def _poll_loop(self, state: InstanceCDPState):
        """Background thread: poll for Chrome, connect, inject."""
        self._update_state(state, "polling", "")

        # Enable WebView debugging on the device (requires root)
        self._enable_debugging(state)

        while not state.stop_event.is_set():
            try:
                # 1. Check if Chrome DevTools socket exists
                socket_name = self._find_devtools_socket(state)
                if not socket_name:
                    self._update_state(state, "polling", "")
                    state.stop_event.wait(CHROME_POLL_INTERVAL)
                    continue

                # 2. Setup ADB port forward
                if not self._setup_forward(state, socket_name):
                    self._update_state(state, "error", "ADB forward failed")
                    state.stop_event.wait(CHROME_POLL_INTERVAL * 2)
                    continue

                # 3. Read device profile
                self._update_state(state, "connecting", "")
                state.profile = read_device_profile(self._adb_exe, state.adb_serial)

                # 4. Discover tabs
                tabs = self._discover_tabs(state.local_port)
                if not tabs:
                    state.stop_event.wait(CHROME_POLL_INTERVAL)
                    continue

                # 5. Connect to first page-type tab and inject
                injected = False
                for tab in tabs:
                    if tab.get("type") != "page":
                        continue
                    if self._inject_tab(state, tab):
                        injected = True
                        break

                if injected:
                    self._update_state(state, "injected", "")
                    # 6. Monitor for tab changes
                    self._monitor_tabs(state)
                else:
                    state.stop_event.wait(CHROME_POLL_INTERVAL)

            except Exception as e:
                self._update_state(state, "error", str(e))
                # Cleanup on error
                if state.client:
                    state.client.disconnect()
                    state.client = None
                state.connected = False
                state.stop_event.wait(CHROME_POLL_INTERVAL * 2)

    def _find_devtools_socket(self, state: InstanceCDPState) -> Optional[str]:
        """Check if Chrome/WebView DevTools socket exists on the device."""
        try:
            cp = subprocess.run(
                [self._adb_exe, "-s", state.adb_serial, "shell",
                 "cat /proc/net/unix 2>/dev/null"],
                capture_output=True, text=True, timeout=5,
            )
            if cp.returncode != 0:
                return None
            for line in cp.stdout.splitlines():
                # Chrome browser socket
                if DEVTOOLS_SOCKET in line and "webview" not in line.lower():
                    return DEVTOOLS_SOCKET
            # Also check for WebView sockets (used by apps with in-app browsers)
            for line in cp.stdout.splitlines():
                if "webview_devtools_remote_" in line:
                    # Extract the full socket name
                    parts = line.strip().split()
                    if parts:
                        sock = parts[-1].strip()
                        # Socket name is after the last @
                        at = sock.rfind("@")
                        if at >= 0:
                            return sock[at + 1 :]
            return None
        except Exception:
            return None

    def _setup_forward(self, state: InstanceCDPState, socket_name: str) -> bool:
        """Set up ADB port forward to Chrome DevTools."""
        try:
            # Remove any stale forward first
            subprocess.run(
                [self._adb_exe, "-s", state.adb_serial, "forward", "--remove",
                 f"tcp:{state.local_port}"],
                capture_output=True, timeout=5,
            )
            # Create fresh forward
            cp = subprocess.run(
                [self._adb_exe, "-s", state.adb_serial, "forward",
                 f"tcp:{state.local_port}", f"localabstract:{socket_name}"],
                capture_output=True, text=True, timeout=5,
            )
            return cp.returncode == 0
        except Exception:
            return False

    def _discover_tabs(self, local_port: int) -> list:
        """GET /json to discover debuggable Chrome tabs."""
        try:
            url = f"http://127.0.0.1:{local_port}/json"
            req = urllib.request.Request(url, headers={"Host": "127.0.0.1"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []

    def _inject_tab(self, state: InstanceCDPState, tab: dict) -> bool:
        """Connect to a single tab and inject the spoofing payload.

        INJECTION ORDER (carefully sequenced for anti-detection):
        1. Connect WebSocket
        2. IMMEDIATE cleanup — Runtime.evaluate to nuke cdc_* on current page
        3. Enable Page domain ONLY (not Network — Network.enable is detectable)
        4. Register persistent JS payload via addScriptToEvaluateOnNewDocument
        5. Override UA + Client Hints (Network.setUserAgentOverride works without Network.enable)
        6. Override geolocation + timezone (Emulation domain, low detection risk)
        7. Reload page to activate the persistent payload
        8. Post-reload delayed cleanup (catches cdc_* re-injection race)
        NOTE: Emulation.setDeviceMetricsOverride is INTENTIONALLY NOT USED.
        It is the single most detectable CDP command. All dimension spoofing
        is handled purely via JS Object.defineProperty overrides in the payload.
        """
        ws_url = tab.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return False

        # Fix WS URL to use our local forwarded port
        # Sometimes the URL has the device-internal address
        ws_url = self._fix_ws_url(ws_url, state.local_port)

        client = CDPClient(ws_url)
        if not client.connect():
            return False

        # ── STEP 1: IMMEDIATE CDP artifact cleanup on current page ──
        # This runs BEFORE anything else. Page.addScriptToEvaluateOnNewDocument
        # only fires on the NEXT document load, but we need cdc_* and
        # navigator.webdriver fixed on the page that's already loaded.
        client.cleanup_cdp_artifacts()

        # ── STEP 2: Enable ONLY Page domain ──
        # Network.enable is REMOVED — it creates detectable side effects
        # (modifies performance.getEntries, PerformanceObserver behavior).
        client.enable_domains()

        # ── STEP 3: Register persistent JS payload ──
        # This fires on every new document before any page JS runs.
        payload = generate_spoof_payload(state.profile)
        identifier = client.inject_script(payload)

        # ── STEP 4: Override UA + Client Hints at network level ──
        # Network.setUserAgentOverride works WITHOUT Network.enable.
        # It only needs the WebSocket connection to be open.
        client.set_user_agent_override(state.profile)

        # ── STEP 5: Override device metrics at compositor level ──
        # REQUIRED — this is the ONLY way to make CSS layout use spoofed
        # dimensions instead of the real 894×540 emulator viewport.
        # Without this: 100vw divs = 894px, matchMedia('(width:411px)') = false,
        # getBoundingClientRect() uses real coords — Cloudflare catches all of it.
        # The JS payload overrides are belt-and-suspenders for the same values.
        try:
            client.set_device_metrics_override(state.profile)
        except Exception:
            pass  # Non-fatal on very old Chrome versions

        # ── STEP 6: Override geolocation ──
        try:
            country = (state.profile.carrier_country or "us").upper()
            seed = _seed_from_fingerprint(state.profile.build_fingerprint)
            gps = gps_for_country(country, seed)
            client.set_geolocation_override(gps["lat"], gps["lon"], gps["acc"])
        except Exception:
            pass  # Non-fatal: geolocation override is nice-to-have

        # ── STEP 7: Override timezone ──
        try:
            tz_to_use = state.profile.timezone
            try:
                tz_cp = subprocess.run(
                    [self._adb_exe, "-s", state.adb_serial, "shell",
                     "su -c 'wget -qO- --timeout=6 "
                     "\"http://ip-api.com/line/?fields=timezone\" 2>/dev/null'"],
                    capture_output=True, text=True, timeout=12
                )
                ip_tz = tz_cp.stdout.strip()
                if ip_tz and "/" in ip_tz:
                    tz_to_use = ip_tz
                    _dbg(f"CDP timezone from device IP lookup: {ip_tz}")
                else:
                    _dbg(f"CDP timezone device lookup empty, using profile: {tz_to_use}")
            except Exception:
                _dbg(f"CDP timezone device lookup failed, using profile: {tz_to_use}")

            if tz_to_use:
                client.set_timezone_override(tz_to_use)
        except Exception:
            pass  # Non-fatal: some older Chrome versions may not support this

        # ── STEP 8: Reload page ──
        # The addScriptToEvaluateOnNewDocument payload fires on the new document.
        client.reload_page()

        # ── STEP 9: Post-reload delayed cleanup ──
        # There's a race condition: CDP may re-inject cdc_* properties AFTER
        # our Section 0 cleanup runs on the new document. A delayed cleanup
        # catches anything that slipped through.
        time.sleep(0.5)
        client.cleanup_cdp_artifacts()

        # Store state
        if state.client:
            state.client.disconnect()
        state.client = client
        state.injected_id = identifier
        state.connected = True
        return True

    def _monitor_tabs(self, state: InstanceCDPState):
        """After injection, keep monitoring for Chrome closure or new tabs."""
        while not state.stop_event.is_set():
            state.stop_event.wait(TAB_MONITOR_INTERVAL)
            if state.stop_event.is_set():
                break

            tabs = self._discover_tabs(state.local_port)
            if not tabs:
                # Chrome closed
                state.connected = False
                if state.client:
                    state.client.disconnect()
                    state.client = None
                state.injected_id = ""
                self._update_state(state, "polling", "")
                return  # Back to outer poll loop

            # Check if our WS connection is still alive
            if state.client and not state.client.connected:
                state.connected = False
                state.client = None
                state.injected_id = ""
                # Try to reconnect to a tab
                for tab in tabs:
                    if tab.get("type") == "page":
                        if self._inject_tab(state, tab):
                            self._update_state(state, "injected", "")
                            break
                else:
                    self._update_state(state, "polling", "")
                    return

    def _enable_debugging(self, state: InstanceCDPState):
        """Enable WebView/Chrome debugging on the device (requires root)."""
        try:
            subprocess.run(
                [self._adb_exe, "-s", state.adb_serial, "shell",
                 "su -c 'setprop debug.enable.webview.devtools 1'"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        # Also try enabling Chrome's command-line flags for debugging
        try:
            subprocess.run(
                [self._adb_exe, "-s", state.adb_serial, "shell",
                 "su -c 'echo chrome --enable-remote-debugging > "
                 "/data/local/tmp/chrome-command-line && "
                 "chmod 644 /data/local/tmp/chrome-command-line'"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    def _remove_forward(self, state: InstanceCDPState):
        """Remove ADB port forwarding."""
        try:
            subprocess.run(
                [self._adb_exe, "-s", state.adb_serial, "forward", "--remove",
                 f"tcp:{state.local_port}"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    @staticmethod
    def _fix_ws_url(ws_url: str, local_port: int) -> str:
        """Fix the WebSocket URL to use our local forwarded port."""
        # Replace host:port in ws://HOST:PORT/... with 127.0.0.1:local_port
        if ws_url.startswith("ws://"):
            rest = ws_url[5:]  # after ws://
            slash = rest.find("/")
            if slash >= 0:
                path = rest[slash:]
                return f"ws://127.0.0.1:{local_port}{path}"
        return ws_url

    def _update_state(self, state: InstanceCDPState, status: str, error_msg: str):
        """Update state and notify GUI."""
        state.status = status
        state.error_msg = error_msg
        self._fire_status(state.instance_name, status, error_msg)

    def _fire_status(self, instance_name: str, status: str, error_msg: str):
        """Fire status change callback."""
        if self._on_status_change:
            try:
                self._on_status_change(instance_name, status, error_msg)
            except Exception:
                pass

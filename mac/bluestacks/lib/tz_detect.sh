#!/system/bin/sh
# tz_detect.sh — IP-based timezone & carrier auto-detection
# Detects geolocation from current public IP (VPN or raw)
# and returns the correct IANA timezone + carrier info.
#
# Usage:
#   . /path/to/tz_detect.sh
#   detect_timezone   # sets TZ_DETECTED, TZ_COUNTRY, TZ_REGION, TZ_CITY, TZ_IP
#   detect_carrier    # sets CARRIER_NAME, CARRIER_MCC_MNC, CARRIER_COUNTRY

# ── Timezone Detection ──────────────────────────────────────────────────────

detect_timezone() {
    TZ_DETECTED=""
    TZ_COUNTRY=""
    TZ_REGION=""
    TZ_CITY=""
    TZ_IP=""

    # Try ip-api.com first (free, no key required, returns timezone directly)
    local json
    json=$(wget -qO- "http://ip-api.com/json/?fields=status,timezone,regionName,city,countryCode,query" 2>/dev/null)

    if [ -n "$json" ]; then
        local status
        status=$(echo "$json" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')

        if [ "$status" = "success" ]; then
            TZ_DETECTED=$(echo "$json" | sed -n 's/.*"timezone":"\([^"]*\)".*/\1/p')
            TZ_COUNTRY=$(echo "$json" | sed -n 's/.*"countryCode":"\([^"]*\)".*/\1/p')
            TZ_REGION=$(echo "$json" | sed -n 's/.*"regionName":"\([^"]*\)".*/\1/p')
            TZ_CITY=$(echo "$json" | sed -n 's/.*"city":"\([^"]*\)".*/\1/p')
            TZ_IP=$(echo "$json" | sed -n 's/.*"query":"\([^"]*\)".*/\1/p')
            return 0
        fi
    fi

    # Fallback: try ipinfo.io (also free, different format)
    json=$(wget -qO- "https://ipinfo.io/json" 2>/dev/null)
    if [ -n "$json" ]; then
        TZ_DETECTED=$(echo "$json" | sed -n 's/.*"timezone":"\([^"]*\)".*/\1/p')
        TZ_COUNTRY=$(echo "$json" | sed -n 's/.*"country":"\([^"]*\)".*/\1/p')
        TZ_REGION=$(echo "$json" | sed -n 's/.*"region":"\([^"]*\)".*/\1/p')
        TZ_CITY=$(echo "$json" | sed -n 's/.*"city":"\([^"]*\)".*/\1/p')
        TZ_IP=$(echo "$json" | sed -n 's/.*"ip":"\([^"]*\)".*/\1/p')
        if [ -n "$TZ_DETECTED" ]; then
            return 0
        fi
    fi

    return 1
}

# ── Carrier Detection ───────────────────────────────────────────────────────
# Maps detected country to a plausible major carrier.
# Used when the IP location doesn't match the profile's hardcoded carrier.

detect_carrier() {
    CARRIER_NAME=""
    CARRIER_MCC_MNC=""
    CARRIER_COUNTRY=""

    # Need TZ_COUNTRY to be set first
    if [ -z "$TZ_COUNTRY" ]; then
        detect_timezone || return 1
    fi

    CARRIER_COUNTRY=$(echo "$TZ_COUNTRY" | tr 'A-Z' 'a-z')

    case "$CARRIER_COUNTRY" in
        us)
            # Rotate through major US carriers for variety
            local _r=$(( $(date +%s) % 3 ))
            case $_r in
                0) CARRIER_NAME="T-Mobile"; CARRIER_MCC_MNC="310260" ;;
                1) CARRIER_NAME="Verizon"; CARRIER_MCC_MNC="311480" ;;
                2) CARRIER_NAME="AT&T"; CARRIER_MCC_MNC="310410" ;;
            esac
            ;;
        gb|uk)
            local _r=$(( $(date +%s) % 4 ))
            case $_r in
                0) CARRIER_NAME="EE"; CARRIER_MCC_MNC="23430" ;;
                1) CARRIER_NAME="Vodafone UK"; CARRIER_MCC_MNC="23415" ;;
                2) CARRIER_NAME="Three UK"; CARRIER_MCC_MNC="23420" ;;
                3) CARRIER_NAME="O2 UK"; CARRIER_MCC_MNC="23410" ;;
            esac
            ;;
        de)
            local _r=$(( $(date +%s) % 3 ))
            case $_r in
                0) CARRIER_NAME="Deutsche Telekom"; CARRIER_MCC_MNC="26201" ;;
                1) CARRIER_NAME="Vodafone"; CARRIER_MCC_MNC="26202" ;;
                2) CARRIER_NAME="O2 DE"; CARRIER_MCC_MNC="26207" ;;
            esac
            ;;
        fr)
            local _r=$(( $(date +%s) % 3 ))
            case $_r in
                0) CARRIER_NAME="Orange"; CARRIER_MCC_MNC="20801" ;;
                1) CARRIER_NAME="SFR"; CARRIER_MCC_MNC="20810" ;;
                2) CARRIER_NAME="Free Mobile"; CARRIER_MCC_MNC="20815" ;;
            esac
            ;;
        jp)
            local _r=$(( $(date +%s) % 3 ))
            case $_r in
                0) CARRIER_NAME="NTT Docomo"; CARRIER_MCC_MNC="44010" ;;
                1) CARRIER_NAME="SoftBank"; CARRIER_MCC_MNC="44020" ;;
                2) CARRIER_NAME="au (KDDI)"; CARRIER_MCC_MNC="44050" ;;
            esac
            ;;
        ca)
            local _r=$(( $(date +%s) % 3 ))
            case $_r in
                0) CARRIER_NAME="Rogers"; CARRIER_MCC_MNC="302720" ;;
                1) CARRIER_NAME="Bell"; CARRIER_MCC_MNC="302610" ;;
                2) CARRIER_NAME="Telus"; CARRIER_MCC_MNC="302220" ;;
            esac
            ;;
        au)
            local _r=$(( $(date +%s) % 3 ))
            case $_r in
                0) CARRIER_NAME="Telstra"; CARRIER_MCC_MNC="50501" ;;
                1) CARRIER_NAME="Optus"; CARRIER_MCC_MNC="50502" ;;
                2) CARRIER_NAME="Vodafone AU"; CARRIER_MCC_MNC="50503" ;;
            esac
            ;;
        br)
            local _r=$(( $(date +%s) % 3 ))
            case $_r in
                0) CARRIER_NAME="Vivo"; CARRIER_MCC_MNC="72406" ;;
                1) CARRIER_NAME="Claro"; CARRIER_MCC_MNC="72405" ;;
                2) CARRIER_NAME="TIM"; CARRIER_MCC_MNC="72402" ;;
            esac
            ;;
        in)
            local _r=$(( $(date +%s) % 3 ))
            case $_r in
                0) CARRIER_NAME="Jio"; CARRIER_MCC_MNC="40588" ;;
                1) CARRIER_NAME="Airtel"; CARRIER_MCC_MNC="40410" ;;
                2) CARRIER_NAME="Vi"; CARRIER_MCC_MNC="40411" ;;
            esac
            ;;
        mx)
            local _r=$(( $(date +%s) % 2 ))
            case $_r in
                0) CARRIER_NAME="Telcel"; CARRIER_MCC_MNC="33402" ;;
                1) CARRIER_NAME="AT&T MX"; CARRIER_MCC_MNC="33404" ;;
            esac
            ;;
        nl)
            CARRIER_NAME="KPN"; CARRIER_MCC_MNC="20408"
            ;;
        es)
            CARRIER_NAME="Movistar"; CARRIER_MCC_MNC="21407"
            ;;
        it)
            CARRIER_NAME="TIM"; CARRIER_MCC_MNC="22201"
            ;;
        se)
            CARRIER_NAME="Telia"; CARRIER_MCC_MNC="24001"
            ;;
        pl)
            CARRIER_NAME="Orange PL"; CARRIER_MCC_MNC="26003"
            ;;
        kr)
            CARRIER_NAME="SK Telecom"; CARRIER_MCC_MNC="45005"
            ;;
        *)
            # Generic fallback — use T-Mobile with US MCC/MNC
            CARRIER_NAME="T-Mobile"
            CARRIER_MCC_MNC="310260"
            CARRIER_COUNTRY="us"
            ;;
    esac

    return 0
}

# ── Apply detected timezone and carrier to system properties ────────────────

apply_detected_network() {
    # Detect timezone from IP
    if detect_timezone; then
        # Apply timezone
        if [ -n "$TZ_DETECTED" ]; then
            resetprop persist.sys.timezone "$TZ_DETECTED" 2>/dev/null
            export TZ="$TZ_DETECTED"
        fi

        # Detect and apply carrier
        if detect_carrier; then
            if [ -n "$CARRIER_NAME" ]; then
                resetprop gsm.operator.alpha "$CARRIER_NAME" 2>/dev/null
                resetprop gsm.sim.operator.alpha "$CARRIER_NAME" 2>/dev/null
            fi
            if [ -n "$CARRIER_MCC_MNC" ]; then
                resetprop gsm.operator.numeric "$CARRIER_MCC_MNC" 2>/dev/null
                resetprop gsm.sim.operator.numeric "$CARRIER_MCC_MNC" 2>/dev/null
            fi
            if [ -n "$CARRIER_COUNTRY" ]; then
                resetprop gsm.sim.operator.iso-country "$CARRIER_COUNTRY" 2>/dev/null
            fi
        fi
        return 0
    fi
    return 1
}

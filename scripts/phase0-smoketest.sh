#!/usr/bin/env bash
# patchbay Phase 0 exit test: every fabric device answers SNMPv3, every API
# answers a read-only call. Credentials come from a site .env file (see the
# private site-config repo); checks with missing credentials are skipped.
#
# usage: phase0-smoketest.sh /path/to/site/.env
set -u

ENV_FILE=${1:?usage: $0 /path/to/site/.env}
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

PASS=0 FAIL=0 SKIP=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; SKIP=$((SKIP+1)); }
hdr()  { printf '\n== %s ==\n' "$1"; }

need() { # need VAR... -> returns 1 (and remembers why) if any is empty/unset
  for v in "$@"; do
    [ -n "${!v:-}" ] || { NEED_MISSING=$v; return 1; }
  done
}

curlq() { curl -ks --connect-timeout 5 --max-time 15 "$@"; }

hdr "reachability (ICMP)"
for var in OPNSENSE_HOST M4300_HOST ICX_HOST VSPHERE_HOST; do
  h=${!var:-}
  [ -n "$h" ] || { skip "$var unset"; continue; }
  if ping -c1 -W2 "$h" >/dev/null 2>&1 || ping -c1 -t2 "$h" >/dev/null 2>&1; then
    ok "ping $h"
  else
    bad "ping $h"
  fi
done

hdr "SNMPv3 (sysName)"
# Prefer a Homebrew net-snmp if present: the OS-bundled one on macOS is 5.6,
# which predates SHA-2 auth protocols (needed for e.g. Netgear M4300 SHA512).
if [ -z "${SNMPGET:-}" ]; then
  for c in /opt/homebrew/opt/net-snmp/bin/snmpget \
           /usr/local/opt/net-snmp/bin/snmpget snmpget; do
    command -v "$c" >/dev/null && SNMPGET=$c && break
  done
fi
if [ -z "${SNMPGET:-}" ]; then
  skip "snmpget not installed (net-snmp) — install and re-run"
elif ! need SNMP_USER SNMP_AUTH_PW SNMP_PRIV_PW; then
  skip "SNMP credentials not set ($NEED_MISSING)"
else
  # Per-device auth protocol: <PREFIX>_SNMP_AUTH_PROTO overrides
  # SNMP_AUTH_PROTO (default SHA). Values as snmpget -a accepts: SHA, SHA-512…
  for prefix in OPNSENSE M4300 ICX; do
    hvar=${prefix}_HOST; h=${!hvar:-}
    [ -n "$h" ] || { skip "$hvar unset"; continue; }
    pvar=${prefix}_SNMP_AUTH_PROTO
    proto=${!pvar:-${SNMP_AUTH_PROTO:-SHA}}
    if out=$("$SNMPGET" -v3 -l authPriv -u "$SNMP_USER" -a "$proto" \
             -A "$SNMP_AUTH_PW" -x AES -X "$SNMP_PRIV_PW" \
             -t 3 -r 1 "$h" sysName.0 2>&1); then
      ok "snmpv3 $h ($proto) — ${out##*: }"
    else
      bad "snmpv3 $h ($proto) — ${out}"
    fi
  done
fi

hdr "OPNsense API"
if need OPNSENSE_HOST OPNSENSE_API_KEY OPNSENSE_API_SECRET; then
  code=$(curlq -o /dev/null -w '%{http_code}' \
         -u "$OPNSENSE_API_KEY:$OPNSENSE_API_SECRET" \
         "https://$OPNSENSE_HOST/api/core/firmware/status")
  [ "$code" = 200 ] && ok "opnsense firmware/status (200)" || bad "opnsense API (HTTP $code)"
else
  skip "OPNsense API credentials not set ($NEED_MISSING)"
fi

hdr "vSphere API"
if need VSPHERE_HOST VSPHERE_USER VSPHERE_PASS; then
  code=$(curlq -o /dev/null -w '%{http_code}' -X POST \
         -u "$VSPHERE_USER:$VSPHERE_PASS" "https://$VSPHERE_HOST/api/session")
  if [ "$code" = 201 ]; then
    ok "vcenter session (201)"
  else
    # pre-7.0u2 REST path
    code2=$(curlq -o /dev/null -w '%{http_code}' -X POST \
            -u "$VSPHERE_USER:$VSPHERE_PASS" \
            "https://$VSPHERE_HOST/rest/com/vmware/cis/session")
    [ "$code2" = 200 ] && ok "vcenter session, legacy path (200)" \
                       || bad "vcenter session (HTTP $code / legacy $code2)"
  fi
else
  skip "vSphere credentials not set ($NEED_MISSING)"
fi

hdr "UniFi Network API"
if need UNIFI_URL UNIFI_API_KEY; then
  code=$(curlq -o /dev/null -w '%{http_code}' -H "X-API-KEY: $UNIFI_API_KEY" \
         "$UNIFI_URL/proxy/network/integration/v1/sites")
  [ "$code" = 200 ] && ok "unifi integration API (200)" || bad "unifi integration API (HTTP $code)"
elif need UNIFI_URL UNIFI_USER UNIFI_PASS; then
  code=$(curlq -o /dev/null -w '%{http_code}' -X POST \
         -H 'Content-Type: application/json' \
         -d "{\"username\":\"$UNIFI_USER\",\"password\":\"$UNIFI_PASS\"}" \
         "$UNIFI_URL/api/login")
  [ "$code" = 200 ] && ok "unifi legacy login (200)" || bad "unifi legacy login (HTTP $code)"
else
  skip "UniFi credentials not set ($NEED_MISSING)"
fi

hdr "phpIPAM API"
if need IPAM_URL IPAM_APP_ID IPAM_TOKEN; then
  body=$(curlq -H "token: $IPAM_TOKEN" "$IPAM_URL/$IPAM_APP_ID/sections/")
  case "$body" in
    *'"success":true'*) ok "phpipam sections" ;;
    *)                  bad "phpipam: ${body:0:120}" ;;
  esac
else
  skip "phpIPAM credentials not set ($NEED_MISSING)"
fi

hdr "legacy-SSH device (KEX negotiation only)"
if [ -n "${ICX_HOST:-}" ]; then
  err=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
        -o KexAlgorithms=+diffie-hellman-group1-sha1 \
        -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
        -o Ciphers=+aes256-cbc,aes128-cbc,3des-cbc \
        "$ICX_HOST" exit 2>&1)
  case "$err" in
    *"Permission denied"*|'') ok "ssh kex negotiates on $ICX_HOST" ;;
    *"no matching"*)          bad "ssh kex still failing on $ICX_HOST" ;;
    *)                        bad "ssh $ICX_HOST: ${err:0:120}" ;;
  esac
else
  skip "ICX_HOST unset"
fi

printf '\n%d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] && [ "$SKIP" -eq 0 ] && echo "Phase 0 exit criteria: MET"
exit "$FAIL"

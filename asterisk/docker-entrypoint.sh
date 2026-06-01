#!/bin/sh
set -e

# Substitute placeholder passwords in config files from environment variables
sed -i "s/CHANGEME_SIP_1001_PASS/${SIP_1001_PASS:-changeme1}/g" /etc/asterisk/pjsip.conf
sed -i "s/CHANGEME_SIP_1002_PASS/${SIP_1002_PASS:-changeme2}/g" /etc/asterisk/pjsip.conf
sed -i "s/CHANGEME_ARI_PASS/${ARI_PASS:-changeme_ari}/g"       /etc/asterisk/ari.conf

mkdir -p /etc/asterisk/keys
if [ ! -f /etc/asterisk/keys/asterisk.pem ] || [ ! -f /etc/asterisk/keys/asterisk.key ]; then
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout /etc/asterisk/keys/asterisk.key \
        -out /etc/asterisk/keys/asterisk.pem \
        -days 3650 \
        -subj "/CN=pbx-ivr-asterisk" >/dev/null 2>&1
    chown asterisk:asterisk /etc/asterisk/keys/asterisk.pem /etc/asterisk/keys/asterisk.key 2>/dev/null || true
    chmod 600 /etc/asterisk/keys/asterisk.key
fi

chmod +x /etc/asterisk/agi-bin/ivr_dynamic.py

exec "$@"

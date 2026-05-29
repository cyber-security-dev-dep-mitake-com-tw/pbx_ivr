#!/bin/sh
set -e

# Substitute placeholder passwords in config files from environment variables
sed -i "s/CHANGEME_SIP_1001_PASS/${SIP_1001_PASS:-changeme1}/g" /etc/asterisk/pjsip.conf
sed -i "s/CHANGEME_SIP_1002_PASS/${SIP_1002_PASS:-changeme2}/g" /etc/asterisk/pjsip.conf
sed -i "s/CHANGEME_ARI_PASS/${ARI_PASS:-changeme_ari}/g"       /etc/asterisk/ari.conf

chmod +x /etc/asterisk/agi-bin/ivr_dynamic.py

exec "$@"

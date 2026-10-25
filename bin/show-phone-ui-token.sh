#!/usr/bin/env bash
set -euo pipefail
TOKEN_FILE="${DAI_PHONE_TOKEN_FILE:-${HOME:?HOME is not set}/.config/dai-phone-ui/token}"
if [[ ! -r "$TOKEN_FILE" ]]; then
  printf '%s\n' "DAI phone token is unavailable. Run ~/dai/bin/setup-phone-ui.py first." >&2
  exit 1
fi
printf '%s\n' "Copy this token into the DAI Mobile page on the S22. Do not paste it into chat or share it."
cat "$TOKEN_FILE"

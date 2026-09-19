# DAI everything aliases - source from ~/.bashrc
# echo 'source ~/dai-assistant/config/dai-aliases.sh' >> ~/.bashrc

alias d='~/dai-assistant/bin/dai'
alias da='~/dai-assistant/bin/dai-ask'
alias dd='~/dai-assistant/bin/dai-do'
alias dl='~/dai-assistant/bin/dai-listen'
alias dh='~/dai-assistant/bin/dai status; ~/dai-assistant/bin/dai heartbeat --json | python3 -c "import json,sys; print(json.load(sys.stdin).get(\"line\",\"\"))"'
alias dhq='~/dai-assistant/bin/dai-headquarters.sh --detached; echo http://127.0.0.1:8799'
alias dlog='~/dai-assistant/bin/dai logs'
alias ddoctor='~/dai-assistant/bin/doctor.sh'
alias de2e='~/dai-assistant/bin/e2e-loop.sh'
alias dmodels='~/dai-assistant/bin/dai models'
alias dtasks='curl -s http://127.0.0.1:8765/v1/tasks | python3 -m json.tool | tail -n 100'
alias dcooldowns='curl -s http://127.0.0.1:11435/v1/status/cooldowns | python3 -m json.tool'

# Everything hotkey helper - bind Ctrl+Alt+Space to dai-ask --listen in Debian Settings > Keyboard > Custom Shortcuts
# Command: /home/user/dai-assistant/bin/dai-ask --listen

# Dashboard as homepage - set in Chromium: http://127.0.0.1:8799
# Tailscale Serve for phone/remote: tailscale serve --bg http://127.0.0.1:8799

echo "DAI aliases loaded: d, da, dd, dl, dh, dhq, dlog, de2e, dmodels"

# How this skill lands in Vellum (after hatch)

1. Hatch/start Vellum from `vendor/vellum-assistant` (owner-approved `./setup.sh`).
2. Copy or symlink this directory into the assistant workspace skills folder:
   `cp -a skills/agent-s-delegate <vellum-workspace>/skills/agent-s-delegate`
3. Ensure `agent-s-worker` is running (`bin/start-spine.sh`).
4. Ask Vellum to load skill `agent-s-delegate` for GUI work.

Do not vendor-copy Agent S source into Vellum. Call the worker over localhost HTTP.

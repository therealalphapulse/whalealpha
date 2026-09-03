# v4 (Bible §8/§12): separates the release step (one-time bootstrap:
# migrations, seeding) from the run step (serving traffic) — v3's
# Procfile only had `worker`, which ran both, inline, on every boot.
#
# `release` runs once per deploy, before `worker` starts. Railway and
# most other Procfile-based platforms support this natively.
release: python -m app_platform.gateway.bootstrap
worker: python main.py

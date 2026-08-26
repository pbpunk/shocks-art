# Host Bridge Operations

The bridge worker is optional until local configuration exists. `Start App.cmd` attempts worker startup after the web app passes its health contract. `Stop App.cmd` stops the worker independently.

If the worker fails, inspect the app-owned Google host worker logs and `data/google_host_worker_status.json`; do not treat a worker failure as permission to restart or replace unrelated processes.

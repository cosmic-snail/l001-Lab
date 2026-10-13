# Step 1 smoke validation summary

The existing validation record reports 19 pytest tests passed and 47 geometry/boolean/STEP smoke checks passed. Tested dependency versions were CadQuery 2.8.0, `cadquery-ocp` 7.9.3.1.1 (OCP module 7.9.3.1), NumPy 2.4.6, and pytest 9.1.1. The minimal pinned direct requirements are listed in [`requirements-smoke.txt`](../requirements-smoke.txt); that file is not a complete environment lock.

This branch contains the smoke source, configuration, tests, and a concise summary only. The original full snapshot was not pushed: raw install/test logs, environment inventories, machine metadata, DSH prompts/responses/provenance, and generated STEP/report artifacts are excluded. The summary is not a substitute for those raw artifacts. No candidate generation or experiment results are claimed.

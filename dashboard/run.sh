#!/bin/sh
# Serves the repository root so the dashboard can read datasets/ in place.
# Then open http://localhost:8787/dashboard/
cd "$(dirname "$0")/.." || exit 1
exec python3 -m http.server 8787

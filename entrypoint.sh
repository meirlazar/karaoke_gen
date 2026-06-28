#!/bin/bash
set -e

# Rebuild the font cache to register any fonts volume-mounted from the host
echo "Updating font cache..."
fc-cache -fv

# Execute the command passed to the container (e.g., uvicorn) as PID 1
exec "$@"

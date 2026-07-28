#!/bin/bash
# entrypoint.sh — Fix shared volume permissions, then drop to application user.
#
# Docker named volumes mount as root:root 755 by default.  The application
# runs as 'ubuntu' and needs write access to /shared for topology export.
# This entrypoint runs as root, fixes ownership once, then execs the CMD
# as ubuntu via runuser.
set -e
chown ubuntu:ubuntu /shared 2>/dev/null || true
exec runuser -u ubuntu -- "$@"

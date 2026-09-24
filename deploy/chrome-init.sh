#!/bin/sh
# Run before Chrome starts: only stale singleton links from a previous container.
set -eu
profile=/config/academic-profile
mkdir -p "$profile"
for name in SingletonLock SingletonCookie SingletonSocket; do
    if [ -L "$profile/$name" ]; then
        rm -- "$profile/$name"
    fi
done

#!/bin/sh

# Place the file on router.

kill -9 `pidof python3`
cd /usr/share/pcat-manager-web/
python3 ./pcat-manager-web-main.py
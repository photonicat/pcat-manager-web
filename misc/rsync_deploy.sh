#!/bin/bash

echo "Sync pcat-web-manager files to /usr/local/share/pcat-manager-web"
cd "$(dirname "$0")"
cd ../
echo "Current dir:`pwd`, sync ing ..."
rsync --delete -za  --exclude "__pycache__/" app/ root@172.16.0.1:/usr/share/pcat-manager-web # in app dir
sleep 1
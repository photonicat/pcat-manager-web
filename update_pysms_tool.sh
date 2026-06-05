#!/bin/bash

# Script to download/update pysms_tool.py and compare with local version

# Define URLs and file paths
GITHUB_URL="https://github.com/c2h2/pysms_tool/raw/refs/heads/main/pysms_tool.py"
LOCAL_FILE="app/pysms_tool.py"
#LOCAL_DIR="app/pysms_tool"
TEMP_FILE="/tmp/pysms_tool.py"

#mkdir $LOCAL_DIR

echo "Downloading latest pysms_tool.py from GitHub..."
# Download the file to temporary location
if wget -O "$TEMP_FILE" "$GITHUB_URL"; then
    echo "Download successful."
else
    echo "Failed to download file from GitHub."
    exit 1
fi

# Check if local file exists
if [ ! -f "$LOCAL_FILE" ]; then
    echo "Local file does not exist. Copying downloaded file..."
    cp "$TEMP_FILE" "$LOCAL_FILE"
    echo "File updated successfully."
    exit 0
fi

# Compare files
echo "Comparing files..."
if cmp -s "$TEMP_FILE" "$LOCAL_FILE"; then
    echo "No changes detected. Files are identical."
    rm "$TEMP_FILE"
    exit 0
else
    echo "Changes detected:"
    echo "=================="
    diff -u "$LOCAL_FILE" "$TEMP_FILE"
    echo "=================="
    
    # Backup current file
    cp "$LOCAL_FILE" "${LOCAL_FILE}.back"
    echo "Backup created: ${LOCAL_FILE}.back"
    
    # Overwrite with GitHub version
    cp "$TEMP_FILE" "$LOCAL_FILE"
    echo "File updated successfully."
    
    # Clean up
    rm "$TEMP_FILE"
    exit 0
fi

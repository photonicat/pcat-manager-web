#!/bin/bash

# find_backslashes.sh - Find problematic backslash characters in templates
# These backslashes often indicate escaped characters that can cause Jinja2 syntax errors

echo "🔍 Searching for backslash characters in templates..."
echo "=================================================="

TEMPLATE_DIRS=("app/templates")
ISSUES_FOUND=0
TOTAL_FILES=0

# Additional template directories that might exist
if [ -d "/usr/share/pcat-manager-web/templates" ]; then
    TEMPLATE_DIRS+=("/usr/share/pcat-manager-web/templates")
fi

if [ -d "app_temp" ]; then
    TEMPLATE_DIRS+=("app_temp")
fi

# Parse command line arguments
SHOW_CONTEXT=false
VERBOSE=false

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --context|-c) SHOW_CONTEXT=true ;;
        --verbose|-v) VERBOSE=true ;;
        --help|-h) 
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Find backslash characters in HTML templates that may cause Jinja2 syntax errors."
            echo ""
            echo "Options:"
            echo "  --context, -c    Show surrounding context for each match"
            echo "  --verbose, -v    Show detailed output for all files"
            echo "  --help, -h       Show this help message"
            echo ""
            echo "Common patterns detected:"
            echo "  \\\"              Escaped double quotes"
            echo "  \\'               Escaped single quotes" 
            echo "  \\&               Escaped HTML entities"
            echo "  \\n, \\t, \\r      Escaped whitespace characters"
            exit 0
            ;;
        *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

echo ""
if [ "$SHOW_CONTEXT" = true ]; then
    echo "📄 Showing context around matches..."
else
    echo "📄 Use --context to show surrounding context"
fi

if [ "$VERBOSE" = true ]; then
    echo "📢 Verbose mode enabled"
fi

echo ""

# Function to process templates in a directory
process_directory() {
    local dir="$1"
    local dir_name=$(basename "$dir")
    
    if [ ! -d "$dir" ]; then
        return
    fi
    
    echo "📁 Checking directory: $dir"
    echo "-----------------------------------"
    
    local files_in_dir=0
    local issues_in_dir=0
    
    while IFS= read -r -d '' file; do
        files_in_dir=$((files_in_dir + 1))
        TOTAL_FILES=$((TOTAL_FILES + 1))
        
        if [ "$VERBOSE" = true ]; then
            echo "  📄 Checking: $(basename "$file")"
        fi
        
        # Search for backslashes in the file
        local matches=""
        if [ "$SHOW_CONTEXT" = true ]; then
            matches=$(grep -n -B1 -A1 '\\' "$file" 2>/dev/null)
        else
            matches=$(grep -n '\\' "$file" 2>/dev/null)
        fi
        
        if [ -n "$matches" ]; then
            issues_in_dir=$((issues_in_dir + 1))
            ISSUES_FOUND=$((ISSUES_FOUND + 1))
            
            echo "  ⚠️  Found backslashes in: $(basename "$file")"
            
            # Show the matches with proper formatting
            if [ "$SHOW_CONTEXT" = true ]; then
                echo "$matches" | while IFS= read -r line; do
                    if [[ "$line" =~ ^[0-9]+-.*\\.*$ ]]; then
                        echo "      🔍 $line"
                    elif [[ "$line" =~ ^[0-9]+:.*\\.*$ ]]; then
                        echo "      ⚠️  $line"
                    elif [[ "$line" =~ ^-- ]]; then
                        echo "      ---"
                    else
                        echo "      📄 $line"
                    fi
                done
            else
                echo "$matches" | while IFS= read -r line; do
                    echo "      ⚠️  $line"
                done
            fi
            echo ""
        elif [ "$VERBOSE" = true ]; then
            echo "    ✅ No backslashes found"
        fi
        
    done < <(find "$dir" -name "*.html" -type f -print0 2>/dev/null)
    
    if [ $files_in_dir -eq 0 ]; then
        echo "  📭 No HTML files found in this directory"
    else
        echo "  📊 Processed $files_in_dir files, found issues in $issues_in_dir files"
    fi
    echo ""
}

# Process all template directories
for dir in "${TEMPLATE_DIRS[@]}"; do
    process_directory "$dir"
done

# Summary
echo ""
echo "=================================================="
echo "📋 SUMMARY"
echo "=================================================="

echo "📁 Directories checked: ${#TEMPLATE_DIRS[@]}"
echo "📄 Total files checked: $TOTAL_FILES"

if [ $ISSUES_FOUND -eq 0 ]; then
    echo "✅ No backslashes found in any templates"
    echo ""
    echo "🎉 All templates look good!"
else
    echo "⚠️  Files with backslashes: $ISSUES_FOUND"
    echo ""
    echo "🛠️  Common fixes:"
    echo "   • Replace escaped quotes (\\\") with proper quote handling"
    echo "   • Use Alpine.js helper functions instead of complex inline expressions"
    echo "   • Convert Jinja2 bracket notation to dot notation"
    echo "   • Avoid mixing quote types in attribute values"
    echo ""
    echo "💡 Run with --context to see surrounding code"
    echo "   Example: $0 --context"
    echo ""
    echo "🔧 To fix these issues:"
    echo "   1. Review each file listed above"
    echo "   2. Replace problematic patterns with safer alternatives"
    echo "   3. Use the enhanced fix_templates.sh script"
    echo "   4. Test templates with: python3 -c \"import jinja2; ...\""
fi

echo ""
echo "ℹ️  Note: Backslashes in templates often indicate escaped characters"
echo "   that can cause 'unexpected char' errors during Jinja2 parsing,"
echo "   especially after HTML minification in production builds."
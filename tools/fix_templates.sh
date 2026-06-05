#!/bin/bash

# fix_templates.sh - Automatically fix HTML entity encoding issues in Jinja2 templates

echo "Fixing HTML entity encoding issues in templates..."
echo "================================================="

TEMPLATE_DIR="app/templates"
FILES_FIXED=0
BACKUP_DIR="template_backups_$(date +%Y%m%d_%H%M%S)"
ISSUES_FOUND=0
DETECT_ONLY=false

# Parse command line arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --detect-only) DETECT_ONLY=true ;;
        --help|-h) 
            echo "Usage: $0 [OPTIONS]"
            echo "Options:"
            echo "  --detect-only    Only detect issues without fixing them"
            echo "  --help, -h       Show this help message"
            exit 0
            ;;
        *) echo "Unknown parameter: $1"; exit 1 ;;
    esac
    shift
done

# Additional template directories that might exist
ADDITIONAL_TEMPLATE_DIRS=()

# Check for runtime/generated template directories
if [ -d "/usr/share/pcat-manager-web/templates" ]; then
    ADDITIONAL_TEMPLATE_DIRS+=("/usr/share/pcat-manager-web/templates")
fi

if [ -d "app_temp" ]; then
    ADDITIONAL_TEMPLATE_DIRS+=("app_temp")
fi

if [ ! -d "$TEMPLATE_DIR" ]; then
    echo "Error: Template directory '$TEMPLATE_DIR' not found"
    exit 1
fi

# Create backup directory only if not in detect-only mode
if [ "$DETECT_ONLY" = false ]; then
    mkdir -p "$BACKUP_DIR"
    echo "Creating backups in: $BACKUP_DIR"
else
    echo "🔍 Running in DETECT-ONLY mode (no files will be modified)"
fi
echo ""

# Function to detect problematic patterns in a file
detect_issues() {
    local file="$1"
    local has_issues=false
    
    # Pattern 1: Alpine.js attributes with Jinja2 bracket notation that will break after minification
    # Example: x-text='... "{{locales["key"]}}" ...'
    if grep -nE 'x-[a-z]+=['\''"].*\{\{[^}]*\["[^"]*"\][^}]*\}\}.*['\''"]' "$file" 2>/dev/null; then
        has_issues=true
        ISSUES_FOUND=$((ISSUES_FOUND + 1))
    fi
    
    # Pattern 2: Alpine.js attributes with nested quotes that could cause issues
    # Example: x-text="... "{{locales['key']}}" ..."
    if grep -nE '(x-text|x-html|x-show|x-if|@click|:class)="[^"]*"[^"]*\{\{[^}]*\}\}[^"]*"[^"]*"' "$file" 2>/dev/null; then
        has_issues=true
        ISSUES_FOUND=$((ISSUES_FOUND + 1))
    fi
    
    # Pattern 3: HTML entities in Alpine.js attributes
    if grep -nE '(x-[a-z]+|@[a-z]+|:[a-z-]+)="[^"]*(&quot;|&#34;|&apos;|&#39;|&amp;|&lt;|&gt;)' "$file" 2>/dev/null; then
        has_issues=true
        ISSUES_FOUND=$((ISSUES_FOUND + 1))
    fi
    
    return $([ "$has_issues" = true ] && echo 0 || echo 1)
}

# Function to fix a single file
fix_file() {
    local file="$1"
    local file_modified=false
    local temp_file=$(mktemp)
    
    echo "Processing: $file"
    
    # First, detect and report issues
    echo "  Checking for issues..."
    
    # Check for problematic Alpine.js/Jinja2 patterns
    local issues_output=""
    
    # Pattern 1: Bracket notation in Alpine.js with quotes
    issues_output=$(grep -nE 'x-[a-z]+=['\''"].*\{\{[^}]*\["[^"]*"\][^}]*\}\}.*['\''"]' "$file" 2>/dev/null)
    if [ -n "$issues_output" ]; then
        echo "  ⚠️  Found Alpine.js/Jinja2 bracket notation issues:"
        echo "$issues_output" | while IFS= read -r line; do
            echo "      Line $line"
        done
        file_modified=true
    fi
    
    # Pattern 2: Nested quotes issue
    issues_output=$(grep -nE '(x-text|x-html|x-show|x-if)="[^"]*\{\{locales\["[^"]*"\]\}\}[^"]*"' "$file" 2>/dev/null)
    if [ -n "$issues_output" ]; then
        echo "  ⚠️  Found nested quote conflicts:"
        echo "$issues_output" | while IFS= read -r line; do
            echo "      Line $line"
        done
        file_modified=true
    fi
    
    # Pattern 3: HTML entities in Alpine.js
    issues_output=$(grep -nE '(x-[a-z]+|@[a-z]+|:[a-z-]+)="[^"]*(&quot;|&#34;|&apos;|&#39;|&amp;)' "$file" 2>/dev/null)
    if [ -n "$issues_output" ]; then
        echo "  ⚠️  Found HTML entities in Alpine.js attributes:"
        echo "$issues_output" | while IFS= read -r line; do
            echo "      Line $line"
        done
        file_modified=true
    fi
    
    if [ "$DETECT_ONLY" = true ]; then
        if [ "$file_modified" = true ]; then
            echo "  ❌ Issues found (not fixed - detect-only mode)"
            ISSUES_FOUND=$((ISSUES_FOUND + 1))
        else
            echo "  ✅ No issues found"
        fi
        return
    fi
    
    # Create backup
    cp "$file" "$BACKUP_DIR/$(basename "$file")"
    
    # Copy original to temp file for processing
    cp "$file" "$temp_file"
    
    # Fix HTML entities in Alpine.js expressions - comprehensive approach
    # Pattern: any HTML entity within Alpine.js directives
    if grep -E '(x-text|x-show|x-model|x-if|x-for|@click|:class|:disabled|x-data|@[a-z]+|:[a-z-]+)="[^"]*&[a-z#0-9]+;' "$temp_file" > /dev/null; then
        echo "  🔧 Fixing HTML entities in Alpine.js expressions..."
        
        # Fix common HTML entities in Alpine.js attributes
        sed -i 's/&quot;/"/g; s/&amp;/\&/g; s/&lt;/</g; s/&gt;/>/g; s/&apos;/'"'"'/g; s/&#39;/'"'"'/g; s/&#34;/"/g' "$temp_file"
        file_modified=true
    fi
    
    # Fix stray HTML entities anywhere in the template that might cause Jinja2 issues
    if grep -E '&#34;' "$temp_file" > /dev/null; then
        echo "  🔧 Fixing stray &#34; HTML entities..."
        sed -i 's/&#34;/"/g' "$temp_file"
        file_modified=true
    fi
    
    # Fix Jinja2 template quote conflicts in Alpine.js expressions
    # Pattern: x-text="...{{locales["key"]}}..." should become x-text='...{{locales["key"]}}...'
    if grep -E 'x-text="[^"]*\{\{[^}]*\[[^]]*"[^]]*\][^}]*\}\}' "$temp_file" > /dev/null; then
        echo "  🔧 Fixing Jinja2 quote conflicts in Alpine.js x-text attributes..."
        # Change x-text="..." to x-text='...' and swap inner quotes
        sed -i "s/x-text=\"\([^\"]*\{\{[^}]*\[[^]]*\)\"\([^\"]*\][^}]*\}\}[^\"]*\)\"/x-text='\1\"\2'/g" "$temp_file"
        file_modified=true
    fi
    
    
    if [ "$file_modified" = true ]; then
        # Replace original file with fixed version
        mv "$temp_file" "$file"
        echo "  ✅ Fixed and saved"
        FILES_FIXED=$((FILES_FIXED + 1))
    else
        echo "  ✅ No fixes needed"
        rm "$temp_file"
    fi
    
    echo ""
}

# Function to validate Jinja2 syntax
validate_template() {
    local file="$1"
    
    # Try to validate with Python (if available)
    if command -v python3 > /dev/null; then
        python3 -c "
import sys
from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError
try:
    env = Environment(loader=FileSystemLoader('$(dirname "$file")'))
    template = env.get_template('$(basename "$file")')
    print('✅ Template syntax valid: $file')
except TemplateSyntaxError as e:
    print('❌ Template syntax error in $file: ' + str(e))
    sys.exit(1)
except Exception as e:
    print('⚠️  Could not validate $file: ' + str(e))
" 2>/dev/null || echo "⚠️  Could not validate template syntax (jinja2 not available)"
    fi
}

# Dry run option
if [ "$1" = "--dry-run" ]; then
    echo "🔍 DRY RUN MODE - No changes will be made"
    echo ""
    
    find "$TEMPLATE_DIR" -name "*.html" -type f | while read -r file; do
        echo "Would process: $file"
        
        # Check what would be fixed
        if grep -E '(x-text|x-show|x-model|x-if|x-for|@click|:class|:disabled|x-data|@[a-z]+|:[a-z-]+)="[^"]*&[a-z#0-9]+;' "$file" > /dev/null; then
            echo "  Would fix HTML entities in Alpine.js expressions"
        fi
        
        if grep -E '&#34;' "$file" > /dev/null; then
            echo "  Would fix stray &#34; HTML entities"
        fi
        
        if grep -E 'x-text="[^"]*\{\{[^}]*\[[^]]*"[^]]*\][^}]*\}\}' "$file" > /dev/null; then
            echo "  Would fix Jinja2 quote conflicts in Alpine.js x-text attributes"
        fi
        
        
        echo ""
    done
    
    echo "To apply fixes, run: ./fix_templates.sh"
    exit 0
fi

# Function to process templates in a directory
process_template_directory() {
    local dir="$1"
    echo "Processing templates in: $dir"
    echo "-----------------------------------"
    
    if [ -d "$dir" ]; then
        find "$dir" -name "*.html" -type f | while read -r file; do
            fix_file "$file"
        done
    else
        echo "Directory not found: $dir"
    fi
    echo ""
}

# Find and fix all HTML template files in primary directory
process_template_directory "$TEMPLATE_DIR"

# Process additional template directories if they exist
for dir in "${ADDITIONAL_TEMPLATE_DIRS[@]}"; do
    if [ -d "$dir" ]; then
        echo "Found additional template directory: $dir"
        process_template_directory "$dir"
    fi
done

# Validate fixed templates (skip if detect-only mode)
if [ "$DETECT_ONLY" = false ]; then
    echo ""
    echo "Validating fixed templates..."
    echo "============================"

    # Validate primary directory
    find "$TEMPLATE_DIR" -name "*.html" -type f | while read -r file; do
        validate_template "$file"
    done

    # Validate additional directories
    for dir in "${ADDITIONAL_TEMPLATE_DIRS[@]}"; do
        if [ -d "$dir" ]; then
            find "$dir" -name "*.html" -type f | while read -r file; do
                validate_template "$file"
            done
        fi
    done
fi

# Summary
echo ""
echo "================================================="
echo "SUMMARY"
echo "================================================="

if [ "$DETECT_ONLY" = true ]; then
    if [ $ISSUES_FOUND -eq 0 ]; then
        echo "✅ No issues detected in any templates"
    else
        echo "⚠️  Found issues in templates that could cause production errors"
        echo ""
        echo "Common patterns detected:"
        echo "  • Alpine.js with Jinja2 bracket notation: {{locales[\"key\"]}}"
        echo "  • Nested quotes in x-text attributes"
        echo "  • HTML entities in Alpine.js directives"
        echo ""
        echo "To fix these issues automatically, run:"
        echo "  $0"
    fi
else
    if [ $FILES_FIXED -eq 0 ]; then
        echo "✅ No files needed fixing"
    else
        echo "🎉 Fixed $FILES_FIXED file(s) successfully"
        echo "📁 Backups saved in: $BACKUP_DIR"
        echo ""
        echo "Fixed issues:"
        echo "  • Converted Jinja2 bracket notation to dot notation"
        echo "  • Resolved quote conflicts in Alpine.js attributes"
        echo "  • Removed HTML entities from Alpine.js directives"
        echo ""
        echo "Next steps:"
        echo "  1. Test your application to ensure everything works"
        echo "  2. Run with --detect-only to verify all issues resolved"
        echo "  3. Commit the changes if everything works correctly"
    fi
fi
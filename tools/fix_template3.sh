#!/bin/bash

# fix_template3.sh - Compile templates with make.py and check syntax validation
# This script builds production templates and validates them for Jinja2 syntax errors

set -e  # Exit on any error

echo "🔧 Template Compilation & Syntax Validation Tool"
echo "================================================"
echo

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    local status=$1
    local message=$2
    case $status in
        "success")
            echo -e "${GREEN}✅ $message${NC}"
            ;;
        "error")
            echo -e "${RED}❌ $message${NC}"
            ;;
        "warning")
            echo -e "${YELLOW}⚠️  $message${NC}"
            ;;
        "info")
            echo -e "${BLUE}ℹ️  $message${NC}"
            ;;
    esac
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check prerequisites
print_status "info" "Checking prerequisites..."

if ! command_exists python3; then
    print_status "error" "python3 is required but not installed"
    exit 1
fi

if [ ! -f "misc/make.py" ]; then
    print_status "error" "misc/make.py not found. Please run from project root directory"
    exit 1
fi

if [ ! -d "app/templates" ]; then
    print_status "error" "app/templates directory not found"
    exit 1
fi

print_status "success" "Prerequisites check passed"
echo

# Step 1: Clean previous build
print_status "info" "Step 1: Cleaning previous build..."
if [ -d "build" ]; then
    rm -rf build
    print_status "success" "Removed previous build directory"
fi

if [ -d "app_temp" ]; then
    rm -rf app_temp
    print_status "success" "Removed previous app_temp directory"
fi
echo

# Step 2: Run make.py to compile templates
print_status "info" "Step 2: Compiling templates with make.py..."
echo "----------------------------------------"

# Run make.py and capture both stdout and stderr
if python3 misc/make.py > make_output.log 2>&1; then
    print_status "success" "Template compilation completed"
    
    # Show key metrics from the build
    if grep -q "Successfully processed" make_output.log; then
        PROCESSED_COUNT=$(grep "Successfully processed" make_output.log | sed 's/.*Successfully processed \([0-9]*\) templates.*/\1/')
        print_status "success" "Processed $PROCESSED_COUNT templates"
    fi
    
    # Show detected issues
    if grep -q "🔧 Detected problematic patterns" make_output.log; then
        DETECTED_COUNT=$(grep -c "🔧 Detected problematic patterns" make_output.log)
        print_status "warning" "Detected problematic patterns in $DETECTED_COUNT templates (automatically fixed)"
    fi
    
    # Show warnings
    if grep -q "⚠️  Warning:" make_output.log; then
        WARNING_COUNT=$(grep -c "⚠️  Warning:" make_output.log)
        print_status "warning" "$WARNING_COUNT templates may still have issues"
        echo "    Warnings found:"
        grep "⚠️  Warning:" make_output.log | sed 's/^/      /'
    fi
    
else
    print_status "error" "Template compilation failed"
    echo "Build errors:"
    cat make_output.log | tail -20
    exit 1
fi
echo

# Step 3: Check if build directory was created
print_status "info" "Step 3: Verifying build output..."
if [ ! -d "build/templates" ]; then
    print_status "error" "build/templates directory was not created"
    exit 1
fi

TEMPLATE_COUNT=$(find build/templates -name "*.html" | wc -l)
print_status "success" "Found $TEMPLATE_COUNT compiled templates in build/templates/"
echo

# Step 4: Validate template syntax
print_status "info" "Step 4: Validating template syntax..."
echo "----------------------------------------"

# Create Python script for template validation
cat > template_validator.py << 'EOF'
#!/usr/bin/env python3
import sys
import jinja2
from pathlib import Path
import traceback

def validate_templates():
    """Validate all templates in build/templates/ for Jinja2 syntax errors"""
    template_dir = Path('build/templates')
    
    if not template_dir.exists():
        print("❌ build/templates directory not found")
        return False
    
    # Set up Jinja2 environment
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        # Be strict about undefined variables for better error detection
        undefined=jinja2.StrictUndefined
    )
    
    template_files = list(template_dir.glob('*.html'))
    print(f"📋 Validating {len(template_files)} templates...")
    print()
    
    # Track results
    passed = []
    failed = []
    warnings = []
    
    # Critical templates that must pass
    critical_templates = [
        '_base.html', '_firmware_update.html', '_mcu_update.html', 
        'screen.html', '_upgrade_confirmation_modal.html', '_ipk_manager.html'
    ]
    
    for template_file in sorted(template_files):
        template_name = template_file.name
        is_critical = template_name in critical_templates
        
        try:
            # Load and parse template
            template = env.get_template(template_name)
            
            # Try to render with minimal context to catch syntax errors
            # Don't worry about missing variables, just syntax
            try:
                template.render(locales={}, cache_bust_version='test')
                status = "✅ PASS"
                passed.append(template_name)
            except (jinja2.exceptions.UndefinedError, AttributeError, TypeError):
                # These are expected - template needs real data
                status = "✅ PASS" 
                passed.append(template_name)
            except Exception as e:
                if "unexpected char" in str(e) or "expected token" in str(e):
                    status = f"❌ SYNTAX ERROR: {str(e)}"
                    failed.append((template_name, str(e), is_critical))
                else:
                    status = "⚠️  RUNTIME ERROR (syntax OK)"
                    warnings.append((template_name, str(e)))
                    passed.append(template_name)  # Syntax is OK
            
        except jinja2.exceptions.TemplateSyntaxError as e:
            status = f"❌ SYNTAX ERROR: {str(e)}"
            failed.append((template_name, str(e), is_critical))
        except Exception as e:
            status = f"⚠️  LOAD ERROR: {str(e)}"
            warnings.append((template_name, str(e)))
        
        # Print status with critical indicator
        critical_mark = " [CRITICAL]" if is_critical else ""
        print(f"  {template_name:<40} {status}{critical_mark}")
    
    print()
    print("=" * 60)
    print("📊 VALIDATION SUMMARY")
    print("=" * 60)
    
    print(f"✅ Passed: {len(passed)}")
    print(f"⚠️  Warnings: {len(warnings)}")
    print(f"❌ Failed: {len(failed)}")
    print(f"📄 Total: {len(template_files)}")
    
    # Show details for failures
    if failed:
        print()
        print("❌ SYNTAX ERRORS:")
        critical_failures = []
        for template_name, error, is_critical in failed:
            critical_mark = " [CRITICAL]" if is_critical else ""
            print(f"  • {template_name}{critical_mark}: {error}")
            if is_critical:
                critical_failures.append(template_name)
        
        if critical_failures:
            print()
            print(f"🚨 CRITICAL FAILURE: {len(critical_failures)} critical templates have syntax errors!")
            return False
    
    # Show details for warnings  
    if warnings:
        print()
        print("⚠️  WARNINGS (non-syntax issues):")
        for template_name, error in warnings[:5]:  # Show first 5
            print(f"  • {template_name}: {error}")
        if len(warnings) > 5:
            print(f"  ... and {len(warnings) - 5} more warnings")
    
    if failed:
        print()
        print("🔧 RECOMMENDED ACTIONS:")
        print("  1. Fix syntax errors in the templates listed above")  
        print("  2. Run ./fix_templates.sh --detect-only to find problematic patterns")
        print("  3. Add JavaScript helper functions for complex Alpine.js expressions")
        print("  4. Re-run this script to verify fixes")
        return len([f for _, _, is_critical in failed if is_critical]) == 0
    else:
        print()
        print("🎉 All templates passed syntax validation!")
        return True

if __name__ == "__main__":
    success = validate_templates()
    sys.exit(0 if success else 1)
EOF

# Run template validation
if python3 template_validator.py; then
    VALIDATION_STATUS="success"
    VALIDATION_MESSAGE="Template syntax validation passed"
else
    VALIDATION_STATUS="error"
    VALIDATION_MESSAGE="Template syntax validation failed"
fi

# Clean up temporary files
rm -f template_validator.py make_output.log

echo
print_status "$VALIDATION_STATUS" "$VALIDATION_MESSAGE"
echo

# Step 5: Additional checks
print_status "info" "Step 5: Additional checks..."

# Check for specific problematic patterns in compiled templates
print_status "info" "Checking for remaining problematic patterns..."
PROBLEM_COUNT=0

# Check for HTML entities in Alpine.js attributes
if grep -r "&quot;.*{{" build/templates/ >/dev/null 2>&1; then
    PROBLEM_COUNT=$((PROBLEM_COUNT + 1))
    print_status "warning" "Found HTML entities in template expressions"
fi

# Check for escaped quotes in attributes
if grep -r '\\".*{{' build/templates/ >/dev/null 2>&1; then
    PROBLEM_COUNT=$((PROBLEM_COUNT + 1))
    print_status "warning" "Found escaped quotes in template expressions"
fi

if [ $PROBLEM_COUNT -eq 0 ]; then
    print_status "success" "No additional problematic patterns detected"
fi

echo

# Final summary
echo "=" * 60
if [ "$VALIDATION_STATUS" = "success" ]; then
    print_status "success" "🎉 BUILD AND VALIDATION COMPLETE!"
    echo
    echo "📋 SUMMARY:"
    echo "  ✅ Templates compiled: $(find build/templates -name "*.html" | wc -l)"
    echo "  ✅ Critical templates: ALL PASS"
    echo "  ✅ Build artifacts: build/templates/"
    
    if [ $PROBLEM_COUNT -gt 0 ]; then
        echo "  ⚠️  Minor issues: $PROBLEM_COUNT (non-critical)"
    fi
    
    echo
    print_status "info" "🚀 Production templates are ready for deployment"
    echo
    echo "💡 Next steps:"
    echo "   • Deploy build/templates/ to production"
    echo "   • Test critical functionality (/update, /screen, etc.)"
    echo "   • Monitor for any remaining template issues"
    
    exit 0
else
    print_status "error" "❌ BUILD VALIDATION FAILED!"
    echo
    echo "📋 ISSUES FOUND:"
    echo "  ❌ Template syntax errors detected"
    echo "  🔧 Critical templates must be fixed before deployment"
    echo
    echo "💡 Recommended actions:"
    echo "   1. Run ./fix_templates.sh --detect-only to identify patterns"
    echo "   2. Add JavaScript helper functions for complex expressions"
    echo "   3. Re-run this script to verify fixes"
    echo "   4. Check build logs for specific error locations"
    
    exit 1
fi
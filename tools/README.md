# Template Tools

This directory contains tools for managing and validating Jinja2 templates in the pcat-manager-web project.

## Scripts

### `fix_templates.sh`
Template analysis and fixing tool for Alpine.js/Jinja2 conflicts.

**Usage:**
```bash
./tools/fix_templates.sh [OPTIONS]
```

**Options:**
- `--detect-only` - Only detect issues, don't fix them
- `--help` - Show help message

**Features:**
- Detects problematic Alpine.js/Jinja2 patterns
- Fixes bracket notation conflicts
- Handles quote mixing issues
- Creates backups before making changes

### `find_backslashes.sh`
Find and analyze backslash characters in templates that may cause syntax errors.

**Usage:**
```bash
./tools/find_backslashes.sh [OPTIONS]
```

**Options:**
- `--context` - Show surrounding context for matches
- `--verbose` - Show detailed output for all files
- `--help` - Show help message

**Features:**
- Searches all template directories
- Identifies escaped characters that break Jinja2
- Shows context around problematic patterns
- Provides fix recommendations

### `fix_template3.sh`
Complete template compilation and syntax validation tool.

**Usage:**
```bash
./tools/fix_template3.sh
```

**Features:**
- Compiles templates using misc/make.py
- Validates all templates for syntax errors
- Tracks critical template status
- Provides detailed error reporting
- Confirms production readiness

## Workflow

1. **Development**: Use `fix_templates.sh --detect-only` to find issues
2. **Fix Issues**: Add JavaScript helper functions for complex expressions
3. **Validation**: Run `fix_template3.sh` to test production build
4. **Production**: Deploy validated templates from `build/templates/`

## Common Issues Fixed

- **Bracket notation**: `{{locales["key"]}}` → `{{locales.key}}`
- **Complex expressions**: `x-text="condition ? '{{...}}' : '{{...}}"` → `x-text="helperFunction()"`
- **Quote conflicts**: Mixed quotes in Alpine.js attributes
- **HTML entity issues**: `&quot;` breaking Jinja2 parsing

## Integration

These tools are designed to work with:
- `misc/make.py` - Production build script
- Alpine.js - Frontend framework
- Jinja2 - Template engine
- HTML minification - Production optimization
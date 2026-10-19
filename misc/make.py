#!/usr/bin/env python3

#@Author    : qichunren

import os
import sys
import pathlib
import uuid
import re

import shutil 

import compileall
import htmlmin

def fix_alpine_attributes_for_minification(html_content):
  """
  Fix Alpine.js attributes to prevent HTML entity issues during minification.
  This prevents conflicts between Alpine.js, Jinja2, and HTML minification.
  """
  # Fix common problematic patterns in Alpine.js with Jinja2 variables
  # Convert bracket notation to dot notation for safer minification
  html_content = re.sub(r'\{\{locales\["([^"]+)"\]\}\}', r'{{locales.\1}}', html_content)
  html_content = re.sub(r"\{\{locales\['([^']+)'\]\}\}", r'{{locales.\1}}', html_content)
  
  # Fix "or" operators that can cause issues during minification
  # Convert: {{locales.key or "default"}} to: {{locales.key if locales.key else "default"}}
  html_content = re.sub(r'\{\{([^}]+)\s+or\s+"([^"]+)"\}\}', r'{{\1 if \1 else "\2"}}', html_content)
  html_content = re.sub(r"\{\{([^}]+)\s+or\s+'([^']+)'\}\}", r"{{\1 if \1 else '\2'}}", html_content)
  
  # Fix problematic nested quotes in Alpine.js attributes
  # Pattern: x-text='...{{locales["key"]}}...' or similar
  alpine_directives = ['x-text', 'x-html', 'x-show', 'x-if', 'x-for', ':class', ':style', '@click', '@submit', '@change']
  for directive in alpine_directives:
    # Fix nested quotes pattern: directive='...{{...["..."]...}}...'
    pattern = rf'{directive}=\'([^\']*)\{{\{{([^}}]*)\["([^"]+)"\]([^}}]*)\}}\}}([^\']*)\''
    replacement = f'{directive}="\\1\'{{{{\\2.\\3\\4}}}}\'\\5"'
    html_content = re.sub(pattern, replacement, html_content)
    
    # Fix nested quotes pattern: directive="...{{...'...'...}}..."
    pattern = rf'{directive}="([^"]*)\{{\{{([^}}]*?)\'([^\']+)\'([^}}]*?)\}}\}}([^"]*)"'
    replacement = f'{directive}=\'\\1{{{{\\2"\\3"\\4}}}}\\5\''
    html_content = re.sub(pattern, replacement, html_content)
  
  # Fix escaped quotes within Jinja2 expressions that will break during minification
  # Look for patterns like: {{...\"...}} and replace with safer alternatives
  html_content = re.sub(r'(\{\{[^}]*)\\"([^}]*\}\})', r'\1"\2', html_content)
  
  return html_content

def post_minify_cleanup(html_content):
  """
  Clean up any HTML entities that minification introduced in Alpine.js attributes.
  This is a safety measure to ensure Alpine.js directives work correctly.
  """
  # Define common HTML entities and their replacements
  html_entities = {
    '&quot;': '"',
    '&#34;': '"',
    '&apos;': "'",
    '&#39;': "'",
    '&amp;': '&',
    '&lt;': '<',
    '&gt;': '>',
    '&#x27;': "'",
    '&#x22;': '"'
  }
  
  # Find all Alpine.js and Vue-like attributes (including single-quoted)
  alpine_patterns = [
    r'((?:x-|@|:)[a-zA-Z-]+)="([^"]*)"',  # Double-quoted attributes
    r"((?:x-|@|:)[a-zA-Z-]+)='([^']*)'"   # Single-quoted attributes
  ]
  
  for pattern in alpine_patterns:
    matches = re.findall(pattern, html_content)
    for attr_name, attr_value in matches:
      if any(entity in attr_value for entity in html_entities.keys()):
        # Replace HTML entities back to their original characters
        clean_value = attr_value
        for entity, replacement in html_entities.items():
          clean_value = clean_value.replace(entity, replacement)
        
        # Replace in the HTML content (handle both quote styles)
        quote_char = '"' if f'{attr_name}="{attr_value}"' in html_content else "'"
        old_attr = f'{attr_name}={quote_char}{attr_value}{quote_char}'
        new_attr = f'{attr_name}={quote_char}{clean_value}{quote_char}'
        html_content = html_content.replace(old_attr, new_attr)
  
  # Additional cleanup for any remaining escaped characters that could cause issues
  # Remove backslashes that might have been introduced during minification
  alpine_attr_pattern = r'((?:x-|@|:)[a-zA-Z-]+)=(["\'])([^"\']*\\[^"\']*)\2'
  
  def fix_escaped_chars(match):
    attr_name = match.group(1)
    quote_char = match.group(2)
    attr_value = match.group(3)
    
    # Remove problematic escaped characters that break Jinja2
    # But preserve necessary escapes in JavaScript regex patterns
    if not any(js_pattern in attr_value for js_pattern in ['.match(', '.replace(', '.test(']):
      # This is likely a template expression, clean it up
      attr_value = attr_value.replace('\\"', '"')
      attr_value = attr_value.replace("\\'", "'")
      # Don't remove \\n, \\t etc. as they might be intentional
      
    return f'{attr_name}={quote_char}{attr_value}{quote_char}'
  
  html_content = re.sub(alpine_attr_pattern, fix_escaped_chars, html_content)
  
  return html_content

if __name__ == "__main__":
  current_working_path = pathlib.Path(__file__).parent.parent.resolve()
  from_directory = current_working_path.joinpath("app")
  from_directory_temp = current_working_path.joinpath("app_temp")
  # to_directory = pathlib.Path("/usr/share/pcat-manager-web")
  to_directory = current_working_path.joinpath("build")

  os.chdir(str(current_working_path))
  print("Current working dir:", current_working_path)
  print("Build dir:", to_directory)

  if from_directory_temp.exists():
    shutil.rmtree(str(from_directory_temp))
  shutil.copytree(str(from_directory), str(from_directory_temp))

  print("\nStep 0.1: Rename template file name")
  new_login_filename    = str(uuid.uuid4()) + ".html"
  new_index_filename    = str(uuid.uuid4()) + ".html"
  new_settings_filename = str(uuid.uuid4()) + ".html"
  new_modem_filename    = str(uuid.uuid4()) + ".html"
  new_timer_filename    = str(uuid.uuid4()) + ".html"
  new_update_filename   = str(uuid.uuid4()) + ".html"
  new_about_filename    = str(uuid.uuid4()) + ".html"
  new_statistics_filename     = str(uuid.uuid4()) + ".html"
  new_device_control_filename = str(uuid.uuid4()) + ".html"
  
  nt_file_name = from_directory_temp.joinpath("nt.py")
  with open(nt_file_name, "r") as f:
    source_code = f.read()
    source_code = source_code.replace("login_file_name = \"login.html\"", "login_file_name = \"{}\"".format(new_login_filename))
    source_code = source_code.replace("index_file_name = \"index.html\"", "index_file_name = \"{}\"".format(new_index_filename))
    source_code = source_code.replace("settings_file_name = \"settings.html\"", "settings_file_name = \"{}\"".format(new_settings_filename))
    source_code = source_code.replace("modem_file_name = \"modem.html\"", "modem_file_name = \"{}\"".format(new_modem_filename))
    source_code = source_code.replace("timer_file_name = \"timer.html\"", "timer_file_name = \"{}\"".format(new_timer_filename))
    source_code = source_code.replace("update_file_name = \"update.html\"", "update_file_name = \"{}\"".format(new_update_filename))
    source_code = source_code.replace("about_file_name = \"about.html\"", "about_file_name = \"{}\"".format(new_about_filename))
    source_code = source_code.replace("statistics_file_name = \"statistics.html\"", "statistics_file_name = \"{}\"".format(new_statistics_filename))
    source_code = source_code.replace("device_control_file_name = \"device_control.html\"", "device_control_file_name = \"{}\"".format(new_device_control_filename))
    f_new = open(str(nt_file_name)+".new", "w")
    f_new.write(source_code)
    f_new.close()
  os.remove(nt_file_name)
  os.rename(str(nt_file_name)+".new", nt_file_name)
  os.rename(from_directory_temp.joinpath("templates/login.html"), from_directory_temp.joinpath("templates/" + new_login_filename))
  os.rename(from_directory_temp.joinpath("templates/index.html"), from_directory_temp.joinpath("templates/" + new_index_filename))
  os.rename(from_directory_temp.joinpath("templates/settings.html"), from_directory_temp.joinpath("templates/" + new_settings_filename))
  os.rename(from_directory_temp.joinpath("templates/modem.html"), from_directory_temp.joinpath("templates/" + new_modem_filename))
  os.rename(from_directory_temp.joinpath("templates/timer.html"), from_directory_temp.joinpath("templates/" + new_timer_filename))
  os.rename(from_directory_temp.joinpath("templates/update.html"), from_directory_temp.joinpath("templates/" + new_update_filename))
  os.rename(from_directory_temp.joinpath("templates/about.html"), from_directory_temp.joinpath("templates/" + new_about_filename))
  os.rename(from_directory_temp.joinpath("templates/statistics.html"), from_directory_temp.joinpath("templates/" + new_statistics_filename))
  os.rename(from_directory_temp.joinpath("templates/device_control.html"), from_directory_temp.joinpath("templates/" + new_device_control_filename))

  print("\nStep 1.1: Compile files ...")
  compileall.compile_dir('app_temp/', force=True, quiet=1)

  if to_directory.exists():
    shutil.rmtree(str(to_directory))

  print("\nStep 1.2: Copy files to build directory.")
  shutil.copytree(str(from_directory_temp.joinpath("__pycache__")), str(to_directory))
  shutil.copytree(str(from_directory_temp.joinpath("pcat_util/__pycache__")), str(to_directory.joinpath("pcat_util")))
  # locales/ is a subpackage (app/locales/{__init__,cn,en}.py); ship its compiled
  # __pycache__ the same way pcat_util is shipped so `import locales` resolves.
  shutil.copytree(str(from_directory_temp.joinpath("locales/__pycache__")), str(to_directory.joinpath("locales")))
  shutil.copytree(str(from_directory_temp.joinpath("static")), str(to_directory.joinpath("static")))
  shutil.copytree(str(from_directory_temp.joinpath("templates")), str(to_directory.joinpath("templates")))

  # The pcat-call-audio daemon (src/callaudio) carries the in-call sample path
  # in C. It is built separately for the aarch64 target -- this only ships an
  # already-built binary if one is present, so a package can still be made on
  # a machine without the cross toolchain. Its absence is not fatal: Flask
  # reports the daemon unavailable and the phone page falls back to the
  # in-process Python bridge.
  callaudio_bin = current_working_path.joinpath("src/callaudio/pcat-call-audio")
  if callaudio_bin.exists():
    shutil.copy2(str(callaudio_bin), str(to_directory.joinpath("pcat-call-audio")))
    os.chmod(str(to_directory.joinpath("pcat-call-audio")), 0o755)
    print("  shipped pcat-call-audio daemon")
  else:
    print("  NOTE: src/callaudio/pcat-call-audio not built; package will fall "
          "back to the Python audio bridge")

  print("\nStep 1.3: Rename pyc files.")
  pyc_files = pathlib.Path(to_directory).glob('**/*.pyc')
  files = [pyc_file for pyc_file in pyc_files if pyc_file.is_file()]
  for pyc_file in files:
    new_file_name = str(pyc_file).replace("cpython-39.", "")
    new_file_name = new_file_name.replace("cpython-310.", "")
    new_file_name = new_file_name.replace("cpython-311.", "")
    new_file_name = new_file_name.replace("cpython-312.", "")
    new_file_name = new_file_name.replace("cpython-313.", "")
    print(new_file_name)
    os.rename(str(pyc_file), new_file_name)

  print("\nStep 2.1: Compress html files with enhanced Alpine.js/Jinja2 protection.")
  html_files = pathlib.Path(to_directory).joinpath("templates").glob('**/*.html')
  files = [html_file for html_file in html_files if html_file.is_file()]
  
  for html_file in files:
    print(f"  Processing: {html_file.name}")
    with open(html_file, "r") as html_f:
      original_content = html_f.read()
      
      # Check for problematic patterns before processing
      has_bracket_notation = bool(re.search(r'\{\{locales\["[^"]+"\]\}\}', original_content))
      has_or_operator = bool(re.search(r'\{\{[^}]+\s+or\s+["\'][^"\']+["\']\}\}', original_content))
      has_escaped_quotes = bool(re.search(r'\\"', original_content))
      
      if any([has_bracket_notation, has_or_operator, has_escaped_quotes]):
        print(f"    🔧 Detected problematic patterns in {html_file.name}")
        if has_bracket_notation:
          print(f"      • Bracket notation: {{{{locales[\"key\"]}}}}")
        if has_or_operator:
          print(f"      • Or operators: {{{{key or \"default\"}}}}")
        if has_escaped_quotes:
          print(f"      • Escaped quotes: \\\"")
      
      # Fix Alpine.js attributes before minification
      processed_content = fix_alpine_attributes_for_minification(original_content)
      
      # Minify with safer settings
      minified = htmlmin.minify(processed_content, 
                               remove_empty_space=True, 
                               remove_comments=True, 
                               remove_optional_attribute_quotes=False, 
                               reduce_empty_attributes=False,
                               reduce_boolean_attributes=False,
                               keep_pre=True)  # Preserve pre-formatted content
      
      # Clean up any HTML entities that might have been introduced
      final_content = post_minify_cleanup(minified)
      
      # Verify no problematic patterns remain
      remaining_issues = bool(re.search(r'(&quot;|&#34;|\\").*\{\{', final_content))
      if remaining_issues:
        print(f"    ⚠️  Warning: {html_file.name} may still have issues after processing")
      
      html_f_new = open(str(html_file)+".new", "w")
      html_f_new.write(final_content)
      html_f_new.close()
      os.remove(html_file)

  new_html_files = pathlib.Path(to_directory).joinpath("templates").glob('**/*.html.new')
  files = [html_file for html_file in new_html_files if html_file.is_file()]
  templates_processed = 0
  
  for html_file in files:
    new_file_name = str(html_file).replace(".html.new", ".html")
    print(f"    ✓ Compressed and processed: {pathlib.Path(new_file_name).name}")
    os.rename(str(html_file), new_file_name)
    templates_processed += 1

  print(f"\n🎉 Successfully processed {templates_processed} templates with Alpine.js/Jinja2 protection!")




#!/usr/bin/env python3
"""Fix the broken translator.py file."""
import re

with open('proxy/translator.py', 'r') as f:
    lines = f.readlines()

# Find the broken line (line 390 has just a quote + newline)
# We need to replace lines 385-391 with the correct content
fixed_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    # Detect the broken system_prompt section
    if i + 5 < len(lines) and 'system_prompt = (' in line and 'Follow the user' in lines[i+1]:
        # Skip the broken lines until we find 'if is_codex:' or 'if not STRIP_TOOL_DEFINITIONS:'
        # Replace with correct content
        fixed_lines.append('                system_prompt = (\n')
        fixed_lines.append('                    "Follow the user\'s instructions carefully.\\n"\n')
        fixed_lines.append('                    "Communicate in the user\'s language, keep technical terms in English.\\n\\n"\n')
        fixed_lines.append('                    "## Tool Calling\\n"\n')
        fixed_lines.append('                    "When you need to use ANY tool, output the tool call in JSON format inside <tool_call XML tags.\\n"\n')
        fixed_lines.append('                    "Output ONE tool call at a time, then WAIT for the result.\\n"\n')
        fixed_lines.append('                )\n')
        fixed_lines.append('\n')
        fixed_lines.append('            parts = [system_prompt]\n')
        fixed_lines.append('            tools_prompt = ""\n')
        fixed_lines.append('            if not STRIP_TOOL_DEFINITIONS:\n')
        # Skip broken lines until we find 'if is_codex:'
        i += 1
        while i < len(lines) and 'if is_codex:' not in lines[i] and 'if not STRIP_TOOL' not in lines[i]:
            i += 1
        # Now lines[i] should be 'if is_codex:' or we already added the if not STRIP line
        if i < len(lines) and 'if is_codex:' in lines[i]:
            fixed_lines.append(lines[i])  # if is_codex:
            i += 1
        continue
    fixed_lines.append(line)
    i += 1

with open('proxy/translator.py', 'w') as f:
    f.writelines(fixed_lines)

print("Fixed successfully")

# Verify
with open('proxy/translator.py', 'r') as f:
    content = f.read()
    
# Basic syntax check
try:
    compile(content, 'translator.py', 'exec')
    print("Syntax check PASSED")
except SyntaxError as e:
    print(f"Syntax error: {e}")
    # Show the area around the error
    lines = content.split('\n')
    for j in range(max(0, e.lineno - 3), min(len(lines), e.lineno + 3)):
        print(f'{j+1}|{lines[j]}')

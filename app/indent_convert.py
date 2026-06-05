#!/usr/bin/env python3
import argparse
import re
import sys


def convert_indentation(text: str) -> str:
    """
    For each line, replace every two leading spaces with four spaces.
    Any odd leftover space is preserved after conversion.
    """
    out_lines = []
    for line in text.splitlines(keepends=True):
        # Match leading spaces
        m = re.match(r'^( *)', line)
        spaces = m.group(1)
        count = len(spaces)
        levels, remainder = divmod(count, 2)
        # New indent: 4 spaces per original 2-space level + any leftover
        new_indent = ' ' * (levels * 4 + remainder)
        out_lines.append(new_indent + line[len(spaces):])
    return ''.join(out_lines)


def process_stream(in_stream, out_stream):
    data = in_stream.read()
    out_stream.write(convert_indentation(data))


def main():
    parser = argparse.ArgumentParser(
        description='Convert 2-space indentation to 4-space indentation.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Usage examples:
  # Convert and print to console
  python3 indent.convert.py source.py

  # Convert and write in place (using a temporary file)
  python3 indent.convert.py source.py -o tmp && mv tmp source.py

  # Convert from stdin and write to stdout
  cat source.py | python3 indent.convert.py > new_source.py
'''    )
    parser.add_argument(
        'input',
        nargs='?',
        type=argparse.FileType('r'),
        default=sys.stdin,
        help='Input file (default: stdin)'
    )
    parser.add_argument(
        '-o', '--output',
        type=argparse.FileType('w'),
        default=sys.stdout,
        help='Output file (default: stdout)'
    )
    args = parser.parse_args()
    process_stream(args.input, args.output)


if __name__ == '__main__':
    main()

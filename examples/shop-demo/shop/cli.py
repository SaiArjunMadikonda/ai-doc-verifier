"""Command line helper: ``shopctl``."""

import argparse


def build_order(items):
    return list(items)


def main():
    parser = argparse.ArgumentParser(prog="shopctl")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--output-format", default="json", help="json or table")
    return parser

#!/usr/bin/env bash
set -euo pipefail

# Keep the commands and their version switches explicit. Several security
# tools do not implement the GNU-style `--version` flag consistently.
required=(curl jq python python3 nmap ffuf katana httpx subfinder nuclei sqlmap)
missing=()
for command_name in "${required[@]}"; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    missing+=("$command_name")
  fi
done
chrome_bin="${CHROME_BIN:-chromium}"
if [ ! -x "${chrome_bin}" ] && ! command -v "${chrome_bin}" >/dev/null 2>&1; then
  missing+=("${chrome_bin}")
fi
if ((${#missing[@]})); then
  printf '[axiom-toolchain] missing: %s\n' "${missing[*]}" >&2
  exit 1
fi

version_args() {
  case "$1" in
    ffuf) printf '%s\n' '-V' ;;
    katana|httpx|subfinder|nuclei) printf '%s\n' '-version' ;;
    *) printf '%s\n' '--version' ;;
  esac
}

printf '[axiom-toolchain] verified\n'
for command_name in "${required[@]}"; do
  flag="$(version_args "$command_name")"
  output=""
  if ! output="$($command_name "$flag" 2>&1)"; then
    printf '[axiom-toolchain] %s %s failed:\n%s\n' "$command_name" "$flag" "$output" >&2
    exit 1
  fi
  version="$(printf '%s\n' "$output" | sed -n '/[^[:space:]]/ {p; q;}')"
  printf '  %-10s %s\n' "$command_name" "${version:-available}"
done
if ! chrome_output="$($chrome_bin --version 2>&1)"; then
  printf '[axiom-toolchain] %s --version failed:\n%s\n' "$chrome_bin" "$chrome_output" >&2
  exit 1
fi
chrome_version="$(printf '%s\n' "$chrome_output" | sed -n '/[^[:space:]]/ {p; q;}')"
printf '  %-10s %s\n' chromium "${chrome_version:-available}"

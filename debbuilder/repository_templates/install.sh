#!/bin/sh
set -eu
@CONFIGURATION@
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer as root.' >&2
  exit 1
fi
keyring=/etc/apt/keyrings/debbuilder.gpg
source_file=/etc/apt/sources.list.d/debbuilder.sources
install -d -m 0755 /etc/apt/keyrings
key_temp=$(mktemp /etc/apt/keyrings/.debbuilder.gpg.XXXXXX)
trap 'rm -f "$key_temp"' EXIT HUP INT TERM
curl -fsSL "@REPOSITORY_URL@/repository.gpg" -o "$key_temp"
key_listing=$(gpg --batch --quiet --with-colons --fingerprint --show-keys "$key_temp")
printf '%s\n' "$key_listing" | awk -F: '$1 == "pub" { count++; primary_next = 1; next } $1 == "fpr" && primary_next { primary = $10; primary_next = 0 } END { exit !(count == 1 && primary == "@FINGERPRINT@") }'
install -m 0644 "$key_temp" "$keyring"
cat > "$source_file" <<'SOURCES'
Types: deb
URIs: @REPOSITORY_URL@
Suites: @SUITE@
Components: @COMPONENT@
Signed-By: /etc/apt/keyrings/debbuilder.gpg
SOURCES
apt-get update

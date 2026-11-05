#!/usr/bin/env bash
# Prints true when VERSION differs from the latest PREFIX tag, as that version has not been released.
# usage: needs-release.sh PREFIX VERSION
set -euo pipefail

prefix=$1
version=$2

if ! [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+.][0-9A-Za-z.-]+)?$ ]]; then
  echo "Not a version: $version" >&2
  exit 1
fi

latest=$(git tag --list "$prefix*" --sort=-v:refname | head -n 1)
if [[ $latest == "$prefix$version" ]]; then
  echo false
elif git rev-parse --verify --quiet "refs/tags/$prefix$version" >/dev/null; then
  # An older version again. Its tag is taken, and a release must never move one.
  echo "$prefix$version is already released, and the latest is $latest." >&2
  echo false
else
  echo true
fi

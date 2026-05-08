#!/usr/bin/env bash
set -euo pipefail

CHART_FILE="charts/paperless-wsd-bridge/Chart.yaml"
VALUES_FILE="charts/paperless-wsd-bridge/values.yaml"
EXAMPLE_VALUES_FILE="charts/paperless-wsd-bridge/values.example.yaml"
CHANGELOG_FILE="CHANGELOG.md"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_file() {
  if [[ ! -f "$1" ]]; then
    echo "Required file not found: $1" >&2
    exit 1
  fi
}

require_cmd git
require_cmd git-cliff
require_cmd helm
require_cmd python3

require_file "${CHART_FILE}"
require_file "${VALUES_FILE}"
require_file "${EXAMPLE_VALUES_FILE}"
require_file "${CHANGELOG_FILE}"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean. Commit or stash changes before releasing." >&2
  exit 1
fi

release_push="${RELEASE_PUSH:-true}"
if [[ "${release_push}" != "false" ]]; then
  if ! git remote get-url origin >/dev/null 2>&1; then
    echo "No usable origin remote is configured. Set RELEASE_PUSH=false or add origin." >&2
    exit 1
  fi
fi

current_version="$(python3 - "${CHART_FILE}" <<'PY'
import sys
from pathlib import Path

for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("version:"):
        print(line.split(":", 1)[1].strip().strip('"'))
        break
else:
    raise SystemExit("Chart version not found")
PY
)"

if [[ -n "${RELEASE_VERSION:-}" ]]; then
  raw_version="${RELEASE_VERSION}"
elif git tag --list 'v[0-9]*' | grep -q .; then
  raw_version="$(git cliff --bumped-version)"
else
  raw_version="${current_version}"
fi

if [[ -z "${raw_version}" ]]; then
  echo "Could not determine next version" >&2
  exit 1
fi

if [[ "${raw_version}" == v* ]]; then
  tag="${raw_version}"
  new_version="${raw_version#v}"
else
  new_version="${raw_version}"
  tag="v${raw_version}"
fi

if [[ ! "${new_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]]; then
  echo "Release version must be SemVer-like: ${new_version}" >&2
  exit 1
fi

if git rev-parse "${tag}" >/dev/null 2>&1; then
  echo "Tag ${tag} already exists" >&2
  exit 1
fi

echo "Current chart version: ${current_version}"
echo "Preparing release: ${tag}"

python3 - "${new_version}" "${CHART_FILE}" "${VALUES_FILE}" "${EXAMPLE_VALUES_FILE}" <<'PY'
import sys
from pathlib import Path

version, chart_file, values_file, example_values_file = sys.argv[1:]


def replace_first_line(path, prefix, replacement):
    file_path = Path(path)
    lines = file_path.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            file_path.write_text("\n".join(lines) + "\n")
            return
    raise SystemExit(f"Could not find line starting with {prefix!r} in {path}")


replace_first_line(chart_file, "version:", f"version: {version}")
replace_first_line(chart_file, "appVersion:", f'appVersion: "{version}"')

for values_path in (values_file, example_values_file):
    replace_first_line(values_path, "  tag:", f'  tag: "{version}"')
PY

echo "Updating ${CHANGELOG_FILE} with git-cliff..."
git cliff --unreleased --tag "${tag}" --prepend "${CHANGELOG_FILE}"
python3 - "${CHANGELOG_FILE}" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
content = path.read_text().rstrip() + "\n"
path.write_text(content)
PY

echo "Validating release files..."
git diff --check
python3 -m compileall -q src
helm lint charts/paperless-wsd-bridge
helm template paperless-wsd-bridge charts/paperless-wsd-bridge >/dev/null

git add "${CHART_FILE}" "${VALUES_FILE}" "${EXAMPLE_VALUES_FILE}" "${CHANGELOG_FILE}"

git commit --no-verify -m "chore(release): prepare for ${new_version} #ignore" || {
  echo "Nothing to commit; aborting" >&2
  exit 1
}

echo "Creating tag ${tag} on HEAD..."
git tag "${tag}"

if [[ "${release_push}" == "false" ]]; then
  echo "RELEASE_PUSH=false; leaving release commit and tag local."
else
  echo "Pushing commit and tag to origin..."
  git push origin HEAD
  git push origin "${tag}"
  echo "Tag push will trigger GitHub Release assets, GHCR image publishing, and Helm registry update."
fi

echo "Done. Released ${tag}."

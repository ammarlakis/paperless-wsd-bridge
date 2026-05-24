# paperless-wsd-bridge

Local WSD push-scan receiver for [Paperless-ngx](https://docs.paperless-ngx.com/).

This service registers scan profiles with a WSD-capable scanner, receives
panel-initiated scan events, retrieves scanned images, builds PDFs, and uploads
them to Paperless through the REST API.

## Project Status

This is an early, hardware-dependent project. It is tested with an Epson
ET-4850 and may need profile or SOAP-template adjustments for other scanners.
Open an issue with scanner model, profile, logs, and the failing WSD operation
if another device behaves differently.

## Features

- WSD panel scanning into Paperless-ngx.
- ConfigMap or directory-driven scan profiles.
- ADF multi-page scans with `ImagesToTransfer=0`.
- Auto source mode with ADF first and platen fallback.
- Manual duplex workflow:
  - `Paperless Duplex Fronts` stages the first pass.
  - `Paperless Duplex Backs` scans the second pass, reverses backs, interleaves
    pages, and uploads one PDF.
- Durable spool directories for ready, failed, in-progress, uploaded, debug, and
  duplex staging files.

## Requirements

- A WSD-capable scanner reachable from the bridge.
- A callback URL that the scanner can reach on TCP port `6666`.
- A Paperless-ngx API URL and API token.
- Python 3.12 for local runs, or Docker/Kubernetes for containerized runs.

## Configuration

The container reads scan profiles from `WSD_PROFILES_DIR`, which defaults to
`/app/profiles`. Each profile is a YAML file. A minimal profile looks like this:

```yaml
id: paperless
name: Paperless
color: RGB24
image_format: jpeg
format: tiff-single-uncompressed
quality: 85
target_folder: /spool
spool:
  root: /spool
  in_progress_dir: in-progress
  ready_dir: ready
  uploaded_dir: uploaded
  failed_dir: failed
  debug_dir: debug
  keep_intermediates: false
  debug_retrieve_payloads: false
post_scan:
  enabled: true
  type: paperless_api
  url_env: PAPERLESS_API_URL
  token_env: PAPERLESS_API_TOKEN
  timeout_seconds: 120
  success_action: delete
  failure_action: move
  fields:
    tags: []
send_email: false
use_pdf: true
use_default_ticket: false
validate_scan_ticket: false
create_scan_job_mode: minimal
paper_size: A4
resolution: 300
input_src: Auto
```

Required runtime settings:

| Variable | Purpose |
| --- | --- |
| `WSD_SCANNER_TARGET` | Scanner WSD endpoint, for example `http://scanner.example.local:80/WSD/DEVICE`. |
| `WSD_CALLBACK_URL` | Full callback URL advertised to the scanner, for example `http://bridge.example.local:6666/wsd`. |
| `PAPERLESS_API_URL` | Paperless base URL, without `/api/documents/post_document/`. |
| `PAPERLESS_API_TOKEN` | Paperless API token used for upload. |
| `WSD_PROFILES_DIR` | Directory containing profile YAML files. Defaults to `/app/profiles`. |

Optional settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `WSD_SCANNER_EP_REF` | unset | Known scanner endpoint reference. Leave unset unless discovery needs help. |
| `WSD_LISTEN_HOST` | `0.0.0.0` | Callback server bind address. |
| `WSD_LISTEN_PORT` | `6666` | Callback server bind port. |
| `WSD_SUBSCRIPTION_TTL_SECONDS` | `900` | WSD event subscription TTL. |
| `WSD_RENEW_INTERVAL_SECONDS` | `600` | Subscription renewal interval. |
| `WSD_RETRY_MIN_SECONDS` | `10` | Initial reconnect delay. |
| `WSD_RETRY_MAX_SECONDS` | `300` | Maximum reconnect delay. |

## Docker

Build the image:

```sh
docker build -t paperless-wsd-bridge .
```

Run it with profiles and spool storage mounted from the host:

```sh
docker run --rm \
  -p 6666:6666 \
  -e WSD_SCANNER_TARGET=http://scanner.example.local:80/WSD/DEVICE \
  -e WSD_CALLBACK_URL=http://bridge.example.local:6666/wsd \
  -e PAPERLESS_API_URL=http://paperless.example.local:8000 \
  -e PAPERLESS_API_TOKEN=replace-me \
  -v "$PWD/profiles:/app/profiles:ro" \
  -v "$PWD/spool:/spool" \
  paperless-wsd-bridge
```

## Helm

The chart lives in `charts/paperless-wsd-bridge`.

Create a values file from the example:

```sh
cp charts/paperless-wsd-bridge/values.example.yaml my-values.yaml
```

Set at least:

- `image.repository`
- `scanner.target`
- `callback.url`
- `paperless.apiUrl`
- `paperless.apiToken.existingSecret` or `paperless.apiToken.value`

Install:

```sh
helm upgrade --install paperless-wsd-bridge \
  charts/paperless-wsd-bridge \
  --values my-values.yaml
```

Profiles are rendered from `profiles` values into a ConfigMap and mounted at
`/app/profiles`.

## Security Notes

- Treat the Paperless API token as a secret. Prefer an existing Kubernetes
  Secret over putting `paperless.apiToken.value` in a values file.
- The WSD callback server is intended for a trusted local network. Do not expose
  it to the public internet.
- If `debug_retrieve_payloads` is enabled, retrieved image payloads are written
  to the spool debug directory and may contain document contents.

## Troubleshooting

- The scanner must be able to connect back to `WSD_CALLBACK_URL`; this is often
  the first problem to check.
- If scans register but do not start, try setting `scanner.endpointReference`
  from the scanner's WSD metadata.
- If an ADF scan produces no pages in `Auto` mode, the bridge retries once with
  `Platen`.
- Failed uploads are moved to the failed spool when `failure_action: move` is
  configured. Ready artifacts are retried on service start when profile metadata
  is available.

## Development

Install dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Basic validation:

```sh
python3 -m compileall src
helm lint charts/paperless-wsd-bridge
helm template paperless-wsd-bridge charts/paperless-wsd-bridge
```

Prepare a release commit, changelog entry, tag, and push:

```sh
just release
```

Pushing the release tag triggers the `Release` GitHub Actions workflow. The
workflow publishes:

- a standalone Linux `amd64` release artifact,
- a multi-arch GHCR Docker image for `linux/amd64` and `linux/arm64`, tagged
  with the release version, `major.minor`, and `latest`,
- a packaged Helm chart attached to the GitHub release as
  `paperless-wsd-bridge-helm-<version>.tgz`,
- a Helm repository update by dispatching `ammarlakis/helm-charts`.

Useful release overrides:

```sh
RELEASE_PUSH=false just release
RELEASE_VERSION=0.1.7 just release
```

The release command requires a clean working tree and uses `git-cliff` to build
`CHANGELOG.md` from conventional commits. The Helm repository dispatch requires
the `UPDATE_REGISTRY_APP_PRIVATE_KEY` repository secret used by the GitHub App
that can run workflows in `ammarlakis/helm-charts`.

## License

GPL-3.0. See [LICENSE](LICENSE).

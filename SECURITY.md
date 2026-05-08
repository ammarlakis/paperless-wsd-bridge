# Security Policy

`paperless-wsd-bridge` is designed for trusted local networks. Do not expose the
WSD callback service directly to the public internet.

## Sensitive Data

- Store Paperless API tokens in environment variables or Kubernetes Secrets.
- Avoid committing real scanner addresses, callback hostnames, API URLs, or
  tokens in Helm values files.
- Disable `debug_retrieve_payloads` unless you need it for troubleshooting;
  debug payloads can contain scanned document contents.

## Reporting

Open a private advisory or contact the maintainer before publishing details for
issues that expose document contents, API tokens, or arbitrary file access.

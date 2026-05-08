#!/usr/bin/env python3
# -*- encoding: utf-8 -*-

import json
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import img2pdf
import requests


DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def spool_config(profile):
    spool = profile.get("spool", {})
    root = Path(spool.get("root", profile.get("target_folder", "./scans")))
    return {
        "root": root,
        "in_progress": root / spool.get("in_progress_dir", "in-progress"),
        "ready": root / spool.get("ready_dir", "ready"),
        "uploaded": root / spool.get("uploaded_dir", "uploaded"),
        "failed": root / spool.get("failed_dir", "failed"),
        "debug": root / spool.get("debug_dir", "debug"),
        "keep_intermediates": bool(spool.get("keep_intermediates", False)),
        "debug_retrieve_payloads": bool(spool.get("debug_retrieve_payloads", False)),
    }


def ensure_spool_dirs(profile):
    cfg = spool_config(profile)
    for key in ("in_progress", "ready", "uploaded", "failed"):
        cfg[key].mkdir(parents=True, exist_ok=True)
    if cfg["debug_retrieve_payloads"]:
        cfg["debug"].mkdir(parents=True, exist_ok=True)


def ensure_spool_dirs_for_profiles(profiles):
    for profile in profiles:
        ensure_spool_dirs(profile)


def recover_abandoned_in_progress_for_profiles(profiles):
    for profile in profiles:
        recover_abandoned_in_progress(profile)


def recover_abandoned_in_progress(profile):
    cfg = spool_config(profile)
    cfg["failed"].mkdir(parents=True, exist_ok=True)
    if not cfg["in_progress"].exists():
        return
    for path in sorted(cfg["in_progress"].iterdir()):
        target = _unique_path(cfg["failed"] / ("abandoned-" + path.name))
        shutil.move(str(path), str(target))
        print("Moved abandoned in-progress scan to failed spool: %s" % target, flush=True)


def create_scan_workspace(profile, file_name):
    cfg = spool_config(profile)
    ensure_spool_dirs(profile)
    work_dir = cfg["in_progress"] / file_name
    work_dir.mkdir(parents=True, exist_ok=False)
    return work_dir


def debug_dir(profile):
    cfg = spool_config(profile)
    if not cfg["debug_retrieve_payloads"]:
        return None
    cfg["debug"].mkdir(parents=True, exist_ok=True)
    return str(cfg["debug"])


def sidecar_path(document_path):
    return Path(str(document_path) + ".json")


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def write_metadata(path, metadata):
    sidecar_path(path).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def read_metadata(path):
    sidecar = sidecar_path(path)
    if not sidecar.exists():
        return {}
    return json.loads(sidecar.read_text())


def publish_artifacts(profile, file_name, source_paths, metadata):
    cfg = spool_config(profile)
    cfg["ready"].mkdir(parents=True, exist_ok=True)
    ready_paths = []
    for source_path in source_paths:
        source = Path(source_path)
        ready_path = _unique_path(cfg["ready"] / source.name)
        shutil.move(str(source), str(ready_path))
        artifact_metadata = {
            **metadata,
            "artifact_name": ready_path.name,
            "artifact_path": str(ready_path),
            "published_at": now_utc(),
        }
        write_metadata(ready_path, artifact_metadata)
        ready_paths.append(ready_path)
    return ready_paths


def cleanup_workspace(profile, work_dir):
    cfg = spool_config(profile)
    if cfg["keep_intermediates"]:
        return
    shutil.rmtree(work_dir, ignore_errors=True)


def process_completed_artifacts(profile, artifact_paths, metadata):
    post_scan = profile.get("post_scan", {})
    if not post_scan.get("enabled", False) or post_scan.get("type", "none") == "none":
        print("Post-scan disabled; leaving %d artifact(s) in ready spool" % len(artifact_paths),
              flush=True)
        return

    for artifact_path in artifact_paths:
        try:
            result = run_post_scan(profile, artifact_path, metadata)
            print("Post-scan succeeded for %s: %s" % (artifact_path, result), flush=True)
            handle_success(profile, artifact_path)
        except Exception as exc:
            print("Post-scan failed for %s: %s" % (artifact_path, exc), flush=True)
            handle_failure(profile, artifact_path, str(exc))


def process_manual_duplex(profile, file_name, image_paths, metadata):
    manual = profile.get("manual_duplex", {})
    if not manual.get("enabled", False):
        return None

    role = manual.get("role")
    if role == "fronts":
        return _stage_manual_duplex_fronts(profile, file_name, image_paths, metadata)
    if role == "backs":
        return _merge_manual_duplex_backs(profile, file_name, image_paths, metadata)

    raise ValueError("Unsupported manual_duplex role: %s" % role)


def process_pending_ready(profiles):
    profile_by_id = {profile["id"]: profile for profile in profiles}
    ready_dirs = {spool_config(profile)["ready"] for profile in profiles}
    for ready_dir in ready_dirs:
        if not ready_dir.exists():
            continue
        for path in sorted(ready_dir.iterdir()):
            if path.suffix.lower() not in DOCUMENT_EXTENSIONS:
                continue
            metadata = read_metadata(path)
            profile_id = metadata.get("profile_id")
            if profile_id not in profile_by_id:
                print("Skipping ready artifact without known profile metadata: %s" % path,
                      flush=True)
                continue
            process_completed_artifacts(profile_by_id[profile_id], [path], metadata)


def run_post_scan(profile, artifact_path, metadata):
    post_scan = profile.get("post_scan", {})
    kind = post_scan.get("type", "none")
    if kind == "paperless_api":
        return upload_to_paperless(profile, artifact_path, metadata)
    raise ValueError("Unsupported post_scan type: %s" % kind)


def upload_to_paperless(profile, artifact_path, metadata):
    post_scan = profile["post_scan"]
    base_url = post_scan.get("url") or os.environ.get(post_scan.get("url_env", "PAPERLESS_API_URL"))
    if not base_url:
        raise RuntimeError("Paperless API URL is not configured")

    token = post_scan.get("token") or os.environ.get(post_scan.get("token_env", "PAPERLESS_API_TOKEN"))
    if not token:
        raise RuntimeError("Paperless API token is not configured")

    endpoint = base_url.rstrip("/") + "/api/documents/post_document/"
    timeout = int(post_scan.get("timeout_seconds", 120))
    data = _paperless_form_data(post_scan, metadata)

    artifact = Path(artifact_path)
    content_type = mimetypes.guess_type(str(artifact))[0] or "application/octet-stream"
    headers = {"Authorization": "Token %s" % token}
    with artifact.open("rb") as f:
        files = {"document": (artifact.name, f, content_type)}
        response = requests.post(endpoint, headers=headers, data=data, files=files, timeout=timeout)
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError("Paperless upload failed: HTTP %s %s" %
                           (response.status_code, response.text[:500]))
    try:
        return response.json()
    except ValueError:
        return response.text.strip()


def handle_success(profile, artifact_path):
    action = profile.get("post_scan", {}).get("success_action", "delete")
    metadata = read_metadata(artifact_path)
    if action == "delete":
        _delete_with_sidecar(artifact_path)
    elif action == "archive":
        _move_with_sidecar(artifact_path, spool_config(profile)["uploaded"])
    elif action == "keep":
        pass
    else:
        raise ValueError("Unsupported post_scan success_action: %s" % action)
    _cleanup_manual_duplex_session(metadata)


def handle_failure(profile, artifact_path, error):
    action = profile.get("post_scan", {}).get("failure_action", "move")
    metadata = read_metadata(artifact_path)
    metadata["post_scan_error"] = error
    metadata["post_scan_failed_at"] = now_utc()
    write_metadata(artifact_path, metadata)

    if action == "move":
        _move_with_sidecar(artifact_path, spool_config(profile)["failed"])
    elif action == "keep":
        return
    else:
        raise ValueError("Unsupported post_scan failure_action: %s" % action)


def _paperless_form_data(post_scan, metadata):
    data = []
    fields = post_scan.get("fields", {})
    for key in ("title", "created", "correspondent", "document_type", "storage_path",
                "archive_serial_number"):
        if key in fields:
            data.append((key, _format_value(fields[key], metadata)))

    for tag in fields.get("tags", []):
        data.append(("tags", _format_value(tag, metadata)))

    for custom_field in fields.get("custom_fields", []):
        data.append(("custom_fields", _format_value(custom_field, metadata)))
    return data


def _format_value(value, metadata):
    if not isinstance(value, str):
        return str(value)
    result = value
    for key, replacement in metadata.items():
        result = result.replace("{{%s}}" % key, str(replacement))
    return result


def _move_with_sidecar(artifact_path, target_dir):
    target_dir.mkdir(parents=True, exist_ok=True)
    artifact = Path(artifact_path)
    target = _unique_path(target_dir / artifact.name)
    shutil.move(str(artifact), str(target))
    sidecar = sidecar_path(artifact)
    if sidecar.exists():
        shutil.move(str(sidecar), str(sidecar_path(target)))


def _delete_with_sidecar(artifact_path):
    artifact = Path(artifact_path)
    artifact.unlink(missing_ok=True)
    sidecar_path(artifact).unlink(missing_ok=True)


def _stage_manual_duplex_fronts(profile, file_name, image_paths, metadata):
    dirs = _manual_duplex_dirs(profile)
    dirs["pending"].mkdir(parents=True, exist_ok=True)

    session_dir = _unique_dir(dirs["pending"] / file_name)
    fronts_dir = session_dir / "fronts"
    fronts_dir.mkdir(parents=True, exist_ok=False)
    staged_images = _move_numbered_images(image_paths, fronts_dir)

    session_metadata = {
        **metadata,
        "manual_duplex_role": "fronts",
        "manual_duplex_stage": "pending",
        "manual_duplex_session": session_dir.name,
        "manual_duplex_front_count": len(staged_images),
        "manual_duplex_fronts_staged_at": now_utc(),
    }
    _write_session_metadata(session_dir, session_metadata)
    print("Staged manual duplex fronts: %d page(s) in %s" %
          (len(staged_images), session_dir), flush=True)
    return [], session_metadata


def _merge_manual_duplex_backs(profile, file_name, image_paths, metadata):
    if not image_paths:
        raise RuntimeError("Manual duplex backs scan did not produce any pages")

    manual = profile.get("manual_duplex", {})
    dirs = _manual_duplex_dirs(profile)
    session_dir = _find_manual_duplex_session(dirs["pending"], manual.get("pairing", "latest"))
    if session_dir is None:
        raise RuntimeError("No pending manual duplex fronts scan found")

    backs_dir = session_dir / "backs"
    if backs_dir.exists():
        shutil.rmtree(backs_dir)
    backs_dir.mkdir(parents=True, exist_ok=False)
    back_images = _move_numbered_images(image_paths, backs_dir)

    try:
        front_images = _sorted_images(session_dir / "fronts")
        if manual.get("reverse_backs", True):
            back_images = list(reversed(back_images))

        merged_images = _interleave_manual_duplex_images(front_images, back_images)
        output_dir = Path(image_paths[0]).parent
        pdf_file_name = output_dir / ("%s.pdf" % file_name)
        with pdf_file_name.open("wb") as f:
            f.write(img2pdf.convert([str(path) for path in merged_images]))
    except Exception:
        shutil.rmtree(backs_dir, ignore_errors=True)
        raise

    merged_dir = _unique_dir(dirs["merged"] / session_dir.name)
    dirs["merged"].mkdir(parents=True, exist_ok=True)
    shutil.move(str(session_dir), str(merged_dir))

    session_metadata = _read_session_metadata(merged_dir)
    artifact_metadata = {
        **metadata,
        "manual_duplex_role": "backs",
        "manual_duplex_session": merged_dir.name,
        "manual_duplex_front_scan_started_at": session_metadata.get("scan_started_at"),
        "manual_duplex_front_count": len(front_images),
        "manual_duplex_back_count": len(back_images),
        "manual_duplex_page_count": len(merged_images),
        "manual_duplex_reverse_backs": bool(manual.get("reverse_backs", True)),
        "manual_duplex_merged_at": now_utc(),
        "manual_duplex_session_cleanup_path": str(merged_dir),
    }
    artifacts = publish_artifacts(profile, file_name, [pdf_file_name], artifact_metadata)
    print("Merged manual duplex scan: %d front page(s), %d back page(s), %d PDF page(s)" %
          (len(front_images), len(back_images), len(merged_images)), flush=True)
    return artifacts, artifact_metadata


def _manual_duplex_dirs(profile):
    manual = profile.get("manual_duplex", {})
    root = spool_config(profile)["root"] / manual.get("stage_dir", "duplex")
    return {
        "root": root,
        "pending": root / manual.get("pending_dir", "pending"),
        "merged": root / manual.get("merged_dir", "merged"),
    }


def _find_manual_duplex_session(pending_dir, pairing):
    if not pending_dir.exists():
        return None
    sessions = [path for path in pending_dir.iterdir() if path.is_dir()]
    if not sessions:
        return None
    reverse = pairing != "oldest"
    return sorted(sessions, key=lambda path: path.stat().st_mtime, reverse=reverse)[0]


def _move_numbered_images(source_paths, target_dir):
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for index, source_path in enumerate(source_paths, start=1):
        source = Path(source_path)
        target = target_dir / ("%04d%s" % (index, source.suffix.lower()))
        shutil.move(str(source), str(target))
        staged.append(target)
    return staged


def _sorted_images(path):
    if not path.exists():
        return []
    return sorted(
        child for child in path.iterdir()
        if child.is_file() and child.suffix.lower() in IMAGE_EXTENSIONS
    )


def _interleave_manual_duplex_images(front_images, back_images):
    if len(back_images) > len(front_images):
        raise RuntimeError("Manual duplex has more back pages (%d) than front pages (%d)" %
                           (len(back_images), len(front_images)))
    if len(front_images) - len(back_images) > 1:
        raise RuntimeError("Manual duplex page count mismatch: %d front pages, %d back pages" %
                           (len(front_images), len(back_images)))

    merged = []
    for index, front_image in enumerate(front_images):
        merged.append(front_image)
        if index < len(back_images):
            merged.append(back_images[index])
    return merged


def _session_metadata_path(session_dir):
    return Path(session_dir) / "session.json"


def _write_session_metadata(session_dir, metadata):
    _session_metadata_path(session_dir).write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )


def _read_session_metadata(session_dir):
    path = _session_metadata_path(session_dir)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _cleanup_manual_duplex_session(metadata):
    session_path = metadata.get("manual_duplex_session_cleanup_path")
    if not session_path:
        return
    path = Path(session_path)
    if path.exists():
        shutil.rmtree(path)
        print("Cleaned manual duplex session: %s" % path, flush=True)


def _unique_dir(path):
    path = Path(path)
    if not path.exists():
        return path

    counter = 1
    while True:
        candidate = path.with_name("%s-%d" % (path.name, counter))
        if not candidate.exists():
            return candidate
        counter += 1


def _unique_path(path):
    path = Path(path)
    if not path.exists() and not sidecar_path(path).exists():
        return path

    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = path.with_name("%s-%d%s" % (stem, counter, suffix))
        if not candidate.exists() and not sidecar_path(candidate).exists():
            return candidate
        counter += 1

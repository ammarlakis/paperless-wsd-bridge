import threading
import time
import argparse
import atexit
import os
import signal
import sys
import yaml
from datetime import timedelta

import wsd_discovery__operations
import wsd_discovery__structures
import wsd_eventing__operations
import wsd_globals
import post_scan
import wsd_scan__events
import wsd_transfer__operations
import wsd_discovery__parsers

active_subscriptions = []
cleanup_done = False
server = None


def unsubscribe_active_subscriptions():
    if active_subscriptions:
        print("Unsubscribing %d WSD subscription(s)..." % len(active_subscriptions), flush=True)

    while active_subscriptions:
        hosted_service, subscription_id, label = active_subscriptions.pop()
        try:
            wsd_eventing__operations.wsd_unsubscribe(hosted_service, subscription_id)
            print("Unsubscribed: %s" % label, flush=True)
        except Exception as exc:
            print("Unsubscribe failed for %s: %s" % (label, exc), flush=True)


def cleanup_subscriptions():
    global cleanup_done
    if cleanup_done:
        return
    cleanup_done = True
    unsubscribe_active_subscriptions()


def handle_stop_signal(signum, frame):
    cleanup_subscriptions()
    sys.exit(128 + signum)


atexit.register(cleanup_subscriptions)
signal.signal(signal.SIGINT, handle_stop_signal)
signal.signal(signal.SIGTERM, handle_stop_signal)


def noop(args):
    print("Nothing to do")


def read_profiles_from_yaml(profiles_dir):
    excluded_files = ["mail_service.yaml"]

    if not os.path.isdir(profiles_dir):
        raise RuntimeError("Profiles directory does not exist: %s" % profiles_dir)

    profile_files = [
        entry.name for entry in os.scandir(profiles_dir)
        if entry.is_file() and entry.name not in excluded_files
        and entry.name.lower().endswith((".yaml", ".yml"))
    ]
    if not profile_files:
        raise RuntimeError("No YAML scan profiles found in %s" % profiles_dir)

    profiles = []
    for file in sorted(profile_files):
        with open(os.path.join(profiles_dir, file)) as yaml_file:
            yaml_object = yaml.load(yaml_file, Loader=yaml.FullLoader)
            if not isinstance(yaml_object, dict):
                raise RuntimeError("Scan profile is not a YAML mapping: %s" % file)
            profiles.append(yaml_object)

    return profiles


def start(args):
    if not args.target:
        raise RuntimeError("Scanner target is required")
    if not args.callback_url and not args.self:
        raise RuntimeError("Either --callback-url or --self is required")

    print("Scanner target: %s" % args.target, flush=True)

    wsd_globals.scan_profiles = read_profiles_from_yaml(args.profiles_dir)
    print("Loaded %d profile(s)" % len(wsd_globals.scan_profiles), flush=True)
    post_scan.ensure_spool_dirs_for_profiles(wsd_globals.scan_profiles)
    post_scan.recover_abandoned_in_progress_for_profiles(wsd_globals.scan_profiles)
    post_scan.process_pending_ready(wsd_globals.scan_profiles)

    start_server_thread(args.listen_host, args.listen_port)

    retry_delay = args.retry_min_seconds
    subscription_ttl = timedelta(seconds=args.subscription_ttl_seconds)

    while True:
        try:
            hosted_service = find_scanner_service(args)
            callback_url = args.callback_url or "http://%s:%d/wsd" % (args.self, args.listen_port)
            print("Callback address: %s" % callback_url, flush=True)
            register_profiles(hosted_service, callback_url, subscription_ttl)
            retry_delay = args.retry_min_seconds
            monitor_subscriptions(args.renew_interval_seconds, subscription_ttl)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print("Scanner subscription loop failed: %s" % exc, flush=True)
            unsubscribe_active_subscriptions()
            clear_active_profile_maps()
            print("Retrying scanner connection in %d second(s)..." % retry_delay, flush=True)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, args.retry_max_seconds)


def find_scanner_service(args):
    if args.ep_ref:
        target_service = wsd_discovery__structures.TargetService()
        target_service.ep_ref_addr = args.ep_ref
        target_service.xaddrs = {args.target}
    else:
        target_service = wsd_discovery__operations.get_device(args.target)

    if target_service is None:
        raise RuntimeError("No WSD device replied at %s" % args.target)

    (target_info, hosted_services) = wsd_transfer__operations.wsd_get(target_service)
    for hosted_service in hosted_services:
        if "wscn:ScannerServiceType" in hosted_service.types:
            return hosted_service
    raise RuntimeError("WSD device did not expose a scan service")


def register_profiles(hosted_service, callback_url, subscription_ttl):
    print("Pushing profiles to device...", flush=True)
    for profile in wsd_globals.scan_profiles:
        client_context = profile["id"]
        result = wsd_scan__events.wsd_scan_available_event_subscribe(hosted_service,
                                                                     profile["name"],
                                                                     client_context,
                                                                     callback_url,
                                                                     subscription_ttl)
        if result is False:
            print("Profile subscription failed: %s" % profile["name"], flush=True)
            continue

        subscription_id, dest_token = result
        if dest_token is not None:
            wsd_scan__events.profile_map[client_context] = profile
            wsd_scan__events.token_map[client_context] = dest_token
            wsd_scan__events.host_map[client_context] = hosted_service
            active_subscriptions.append((
                hosted_service,
                subscription_id,
                "%s scan-available" % profile["name"],
            ))
            print("Profile registered: %s" % profile["name"], flush=True)

    if not active_subscriptions:
        raise RuntimeError("No scan profiles were registered")
    print("Waiting for device-initiated scans...", flush=True)


def monitor_subscriptions(renew_interval_seconds, subscription_ttl):
    while True:
        time.sleep(renew_interval_seconds)
        print("Renewing %d WSD subscription(s)..." % len(active_subscriptions), flush=True)
        for hosted_service, subscription_id, label in list(active_subscriptions):
            renewed = wsd_eventing__operations.wsd_renew(hosted_service,
                                                         subscription_id,
                                                         subscription_ttl)
            if not renewed:
                raise RuntimeError("Subscription renewal failed for %s" % label)
        print("WSD subscriptions renewed", flush=True)


def clear_active_profile_maps():
    wsd_scan__events.profile_map.clear()
    wsd_scan__events.token_map.clear()
    wsd_scan__events.host_map.clear()


def start_server_thread(listen_host, listen_port):
    t = threading.Thread(target=start_server, args=(listen_host, listen_port))
    t.daemon = True
    t.start()


def start_server(listen_host, listen_port):
    global server
    print("Starting callback server on %s:%d" % (listen_host, listen_port), flush=True)
    context = {"queues": wsd_scan__events.QueuesSet()}
    server = wsd_scan__events.HTTPServerWithContext((listen_host, listen_port),
                                                    wsd_scan__events.RequestHandler,
                                                    context)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description='WSD Scan')

    parser.set_defaults(func=noop)
    subparsers = parser.add_subparsers()

    list_parser = subparsers.add_parser("start")
    list_parser.add_argument('-t', '--target', default=os.environ.get("WSD_SCANNER_TARGET"),
                             type=str, help="WSD device endpoint URL")
    list_parser.add_argument('-s', '--self', default=os.environ.get("WSD_CALLBACK_HOST"),
                             type=str, help="Callback host/IP used if --callback-url is not set")
    list_parser.add_argument('--callback-url', default=os.environ.get("WSD_CALLBACK_URL"),
                             type=str, help="Full callback URL advertised to the scanner")
    list_parser.add_argument('--ep-ref', default=os.environ.get("WSD_SCANNER_EP_REF"),
                             type=str, help="Known scanner endpoint reference")
    list_parser.add_argument('--listen-host', default=os.environ.get("WSD_LISTEN_HOST", "0.0.0.0"),
                             type=str, help="HTTP callback bind host")
    list_parser.add_argument('--listen-port', default=int(os.environ.get("WSD_LISTEN_PORT", "6666")),
                             type=int, help="HTTP callback bind port")
    list_parser.add_argument('--profiles-dir', default=os.environ.get("WSD_PROFILES_DIR", "./profiles"),
                             type=str, help="Directory containing profile YAML files")
    list_parser.add_argument('--subscription-ttl-seconds',
                             default=int(os.environ.get("WSD_SUBSCRIPTION_TTL_SECONDS", "900")),
                             type=int, help="WSD event subscription TTL")
    list_parser.add_argument('--renew-interval-seconds',
                             default=int(os.environ.get("WSD_RENEW_INTERVAL_SECONDS", "600")),
                             type=int, help="Seconds between subscription renewals")
    list_parser.add_argument('--retry-min-seconds',
                             default=int(os.environ.get("WSD_RETRY_MIN_SECONDS", "10")),
                             type=int, help="Initial scanner reconnect delay")
    list_parser.add_argument('--retry-max-seconds',
                             default=int(os.environ.get("WSD_RETRY_MAX_SECONDS", "300")),
                             type=int, help="Maximum scanner reconnect delay")
    list_parser.set_defaults(func=start)

    args = parser.parse_args()

    args.func(args)


if __name__ == "__main__":
    main()

import collections
import glob
import json
import os
from urllib.parse import urlsplit

ROOT = os.path.join("output", "slotautoplay", "moon-sisters")
INTERESTING = {
    "round_request",
    "network_candidate",
    "round_response",
    "unparsed_response",
    "round_result",
    "round_timeout",
    "round_error",
}

def get_value(mapping, *names):
    if not isinstance(mapping, dict):
        return None
    wanted = {name.lower() for name in names}
    for key, value in mapping.items():
        if key.lower() in wanted:
            return value
    return None

def safe_url(value):
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    return parsed.netloc + parsed.path

def body_metadata(value):
    if not isinstance(value, str):
        return 0, ""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return len(value.encode("utf-8")), ""
    if isinstance(parsed, dict):
        return len(value.encode("utf-8")), ",".join(sorted(parsed))
    return len(value.encode("utf-8")), ""

def main():
    files = sorted(glob.glob(os.path.join(ROOT, "*.jsonl")))
    print(f"files={len(files)}")
    for path in files:
        events = []
        invalid = 0
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    invalid += 1
        counts = collections.Counter(event.get("type", "missing") for event in events)
        summary = ",".join(f"{key}:{value}" for key, value in sorted(counts.items()))
        print(f"FILE {os.path.basename(path)} events={len(events)} invalid={invalid} types={summary}")
        for event in events:
            if event.get("type") not in INTERESTING:
                continue
            request = get_value(event, "request") or {}
            response = get_value(event, "response") or {}
            value = (
                get_value(request, "url")
                or get_value(response, "url")
                or get_value(event, "url")
            )
            body = get_value(response, "rawJson", "body")
            body_length, body_keys = body_metadata(body)
            print(
                "  "
                f"type={get_value(event, 'type') or ''} "
                f"method={get_value(request, 'method') or get_value(event, 'method') or ''} "
                f"status={get_value(response, 'status') or get_value(event, 'status') or ''} "
                f"contentType={get_value(response, 'contentType') or get_value(event, 'contentType') or ''} "
                f"bodyBytes={get_value(response, 'bodyBytes') or ''} "
                f"rawJsonBytes={body_length} "
                f"bodyKeys={body_keys} "
                f"url={safe_url(value)}"
            )

if __name__ == "__main__":
    main()
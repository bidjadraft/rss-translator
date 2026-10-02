import os
import json
from curl_cffi import requests

ENDPOINT = "https://substack.com/api/v1/comment/feed"

COOKIES_PATH = os.getenv("SUBSTACK_COOKIES_PATH", "cookies_simple.json")
COOKIES_RAW = os.getenv("SUBSTACK_COOKIES")


def load_cookies():
    if COOKIES_RAW:
        return json.loads(COOKIES_RAW)
    with open(COOKIES_PATH) as f:
        return json.load(f)


def build_content(text, source_url=None, source_name=None):
    content = []
    for line in text.split("\n"):
        if line.strip():
            content.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": line.strip()}],
                }
            )
    if source_url:
        label = source_name or source_url
        content.append(
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": label,
                        "marks": [
                            {
                                "type": "link",
                                "attrs": {"href": source_url, "underline": True},
                            }
                        ],
                    }
                ],
            }
        )
    return content


def publish_note(text, source_url=None, source_name=None):
    cookies = load_cookies()
    note_data = {
        "bodyJson": {
            "type": "doc",
            "attrs": {"schemaVersion": "v1"},
            "content": build_content(text, source_url, source_name),
        },
        "tabId": "for-you",
        "replyMinimumRole": "everyone",
    }
    r = requests.post(
        ENDPOINT,
        json=note_data,
        cookies=cookies,
        impersonate="chrome",
        headers={
            "Origin": "https://substack.com",
            "Referer": "https://substack.com/notes",
        },
    )
    if r.status_code == 200:
        print("Substack note published")
        return True
    print("Substack note failed:", r.status_code, r.text[:200])
    return False

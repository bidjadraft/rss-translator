import os
import feedparser
import time
import logging
import re
import requests
import configparser
import json
from urllib.parse import urlparse
from datetime import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom

from substack_note import publish_note

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
RSS_DIR = os.path.join(BASE_DIR, "rss")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(RSS_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")
FEEDS_FILE = os.path.join(CONFIG_DIR, "feeds.txt")
TRACKER_FILE = os.path.join(CONFIG_DIR, "last_post.json")

config = configparser.ConfigParser()
config.read(CONFIG_FILE, encoding='utf-8')

# ===== المفاتيح: من متغيرات البيئة (GitHub Secrets) فقط =====

OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")
MASTODON_ACCESS_TOKEN = os.getenv("MASTODON_ACCESS_TOKEN")

# ===== إعدادات Mastodon: الخادم الرسمي مثبّت في الكود =====

MASTODON_API_BASE_URL = "https://mastodon.social"
MASTODON_CHAR_LIMIT = 500

# ===== النماذج =====

models_raw = config.get('models', 'ollama_models', fallback='gpt-oss:120b-cloud')
if ',' in models_raw:
    OLLAMA_MODELS = [m.strip() for m in models_raw.split(',') if m.strip()]
elif '\n' in models_raw:
    OLLAMA_MODELS = [m.strip() for m in models_raw.split('\n') if m.strip() and not m.strip().startswith('[')]
else:
    OLLAMA_MODELS = [models_raw.strip()] if models_raw.strip() else []

if not OLLAMA_MODELS:
    logging.error("❌ No Ollama models found in config.ini")
    exit(1)

logging.info(f"📋 Loaded {len(OLLAMA_MODELS)} Ollama models")

# ===== الإعدادات =====

LANGUAGE = config.get('settings', 'language', fallback='arabic')
CONTENT_TYPE = config.get('settings', 'type', fallback='summary')
MAX_POSTS = config.getint('settings', 'max_post', fallback=20)
MERGE_FEEDS = config.get('settings', 'merge_feeds', fallback='yes').lower() == 'yes'

logging.info(f"🌐 Language: {LANGUAGE} | Type: {CONTENT_TYPE}")
logging.info(f"📊 Max posts: {MAX_POSTS} | Merge: {MERGE_FEEDS}")

# ===== الخلاصات =====

RSS_FEEDS = []
if os.path.exists(FEEDS_FILE):
    with open(FEEDS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            feed_url = line.strip()
            if feed_url and not feed_url.startswith('#') and feed_url.startswith('http'):
                RSS_FEEDS.append(feed_url)
else:
    with open(FEEDS_FILE, 'w', encoding='utf-8') as f:
        f.write("# RSS Feeds List\nhttps://feed.alternativeto.net/news/all\n")
    RSS_FEEDS = ["https://feed.alternativeto.net/news/all"]
    logging.info("📝 Created default feeds.txt")

if not RSS_FEEDS:
    logging.error("❌ No RSS feeds configured in config/feeds.txt")
    exit(1)

logging.info(f"📡 Loaded {len(RSS_FEEDS)} RSS feeds")

USER_AGENT_HEADER = {'User-Agent': 'Mozilla/5.0'}

# ===================== أدوات =====================

class OllamaModelSwitcher:
    def __init__(self, models):
        self.models = models
        self.current_index = 0
        self.all_models_failed = False

    def get_current_model(self):
        return self.models[self.current_index]

    def get_next_model(self):
        if self.current_index < len(self.models) - 1:
            self.current_index += 1
            return self.models[self.current_index]
        self.all_models_failed = True
        return None

    def reset(self):
        self.current_index = 0
        self.all_models_failed = False

def load_tracker():
    if os.path.exists(TRACKER_FILE):
        try:
            with open(TRACKER_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"❌ Error reading tracker: {e}")
    return {}

def save_tracker(tracker_data):
    try:
        with open(TRACKER_FILE, 'w', encoding='utf-8') as f:
            json.dump(tracker_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"❌ Failed to write tracker: {e}")

def normalize_url(url):
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    while url.endswith('//'):
        url = url[:-1]
    if url.endswith('/'):
        url = url[:-1]
    return url

def extract_feed_name(feed_url, feed_data=None):
    try:
        if feed_data and hasattr(feed_data, 'feed'):
            if hasattr(feed_data.feed, 'title') and feed_data.feed.title:
                name = re.sub(r'[^\w\s-]', '', feed_data.feed.title)
                name = name.strip().replace(' ', '_').lower()
                if name and len(name) < 50:
                    return name

        parsed = urlparse(feed_url)
        domain = parsed.netloc.replace('www.', '')
        parts = domain.split('.')
        if len(parts) >= 2:
            name = parts[-2] if parts[-2] not in ['com', 'org', 'net', 'io', 'co'] \
                else (parts[-3] if len(parts) >= 3 else parts[0])
        else:
            name = parts[0]

        return re.sub(r'[^\w\s-]', '', name).strip().lower()
    except Exception as e:
        logging.warning(f"⚠️ Failed to extract feed name: {e}")
        return f"feed_{abs(hash(feed_url)) % 10000}"

def clean_html(raw_html):
    return re.sub(r'<[^>]+>', '', raw_html).strip()

def load_existing_entries(feed_name):
    if MERGE_FEEDS:
        xml_file = os.path.join(RSS_DIR, "merged.xml")
    else:
        xml_file = os.path.join(RSS_DIR, f"{feed_name}.xml")

    existing_entries = []

    if os.path.exists(xml_file):
        try:
            tree = ET.parse(xml_file)
            channel = tree.getroot().find('channel')

            if channel is not None:
                for item in channel.findall('item'):
                    desc_raw = item.findtext('description', '')
                    desc_clean = desc_raw.split('<br><br>المصدر:')[0]

                    entry = {
                        'title': item.findtext('title', ''),
                        'translated_title': item.findtext('title', ''),
                        'link': item.findtext('link', ''),
                        'published': item.findtext('pubDate', ''),
                        'processed_text': desc_clean,
                        'feed_source': item.findtext('source', '') or feed_name
                    }

                    enclosure = item.find('enclosure')
                    if enclosure is not None:
                        entry['image_url'] = enclosure.get('url', '')

                    existing_entries.append(entry)

                logging.info(f"📖 Loaded {len(existing_entries)} existing entries")
        except Exception as e:
            logging.error(f"❌ Error loading existing XML: {e}")

    return existing_entries

def extract_image_from_url(url):
    try:
        response = requests.get(url, headers=USER_AGENT_HEADER, timeout=15)
        response.raise_for_status()
        html = response.text

        patterns = [
            r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
            r'<meta[^>]*name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*name=["\']twitter:image["\']',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                image_url = match.group(1)
                if image_url.startswith('http'):
                    logging.info(f"🖼️ Found image: {image_url}")
                    return image_url

        return None
    except Exception as e:
        logging.error(f"❌ Failed to extract image from {url}: {e}")
        return None

# ===================== Ollama =====================

def translate_title(title, model_switcher):
    if LANGUAGE == 'english':
        return title

    prompt = (f"Translate the following title to {LANGUAGE}. "
              f"Return ONLY the translated title without any additional text.\n\n"
              f"Title: {title}")

    url = "https://ollama.com/api/generate"
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"}

    attempted = 0
    while attempted < len(OLLAMA_MODELS):
        current_model = model_switcher.get_current_model()
        attempted += 1

        try:
            r = requests.post(url, headers=headers,
                              json={"model": current_model, "prompt": prompt, "stream": False},
                              timeout=30)
            if r.status_code == 200:
                translated = r.json().get("response", "").strip()
                if translated:
                    return translated

            logging.warning(f"⚠️ Title translation failed with {current_model}")
        except Exception as e:
            logging.error(f"❌ Title translation error with {current_model}: {e}")

        if not model_switcher.get_next_model():
            break

    return title

def process_with_ollama(text, model_switcher):
    if not OLLAMA_API_KEY:
        logging.error("OLLAMA_API_KEY is not set.")
        return None

    if CONTENT_TYPE == 'translate':
        prompt = f"""Translate the following text to {LANGUAGE}. Translate it completely and accurately.

IMPORTANT RULES:

1. Translate the FULL text without summarizing or shortening
2. Do NOT add any hashtags
3. Return ONLY the translation without any additional comments
4. Preserve the original meaning accurately
5. If translating to Arabic, make sure the translation is natural and fluent

Original text: {text}"""
    else:
        prompt = f"""Summarize the following text in one paragraph in {LANGUAGE}. Keep it between 50-70 words. Include key details and do not be too brief.

IMPORTANT RULES:

1. Write the summary in {LANGUAGE} language
2. Do NOT add any hashtags
3. Return ONLY the summary text without any additional comments or notes
4. Keep the total text under 500 characters
5. If summarizing in Arabic, start with an Arabic word, not an English word or company name

Example for Arabic summary:
Correct: "أعلنت شركة جوجل اليوم عن تحديث جديد لمتصفح كروم يضيف ميزات أمان متطورة…"
Wrong: "Google أعلنت اليوم عن تحديث…"

Original text: {text}"""

    url = "https://ollama.com/api/generate"
    headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"}

    attempted = 0
    while attempted < len(OLLAMA_MODELS):
        current_model = model_switcher.get_current_model()
        attempted += 1

        logging.info(f"🔧 Ollama attempt {attempted}/{len(OLLAMA_MODELS)}: {current_model}")

        try:
            r = requests.post(url, headers=headers,
                              json={"model": current_model, "prompt": prompt, "stream": False},
                              timeout=90)

            if r.status_code != 200:
                logging.error(f"❌ Ollama API error with {current_model}: {r.status_code}")
                if not model_switcher.get_next_model():
                    return None
                continue

            result = r.json().get("response", "").strip()
            if not result:
                logging.error(f"❌ Empty response from {current_model}")
                if not model_switcher.get_next_model():
                    return None
                continue

            result = re.sub(r'#\w+\s*', '', result).strip()

            if len(result) > 500:
                logging.warning(f"⚠️ Text exceeds 500 chars ({len(result)}). Truncating...")
                result = result[:497] + "..."

            logging.info(f"✅ Processing successful | {len(result)} chars")
            return result

        except Exception as e:
            logging.error(f"❌ Ollama API failed with {current_model}: {e}")
            if not model_switcher.get_next_model():
                return None
            continue

    return None

# ===================== Mastodon (منشورات خاصة direct) =====================

def upload_media_to_mastodon(image_url):
    """رفع الصورة إلى mastodon.social وإرجاع معرفها — بدون أي فلترة محتوى"""
    if not MASTODON_ACCESS_TOKEN:
        return None
    try:
        r = requests.get(image_url, headers=USER_AGENT_HEADER, timeout=10)
        r.raise_for_status()

        files = {'file': ('image.jpg', r.content)}
        headers = {"Authorization": f"Bearer {MASTODON_ACCESS_TOKEN}"}

        r2 = requests.post(f"{MASTODON_API_BASE_URL}/api/v2/media",
                           headers=headers, files=files, timeout=20)
        r2.raise_for_status()

        return r2.json().get('id')
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Failed to upload Mastodon media: {e}")
        return None

def post_to_mastodon(text, image_url=None):
    """نشر النص على mastodon.social كمنشور مباشر (direct) لا يراه أحد سواك
    - النص فقط (processed_text) بدون سطر المصدر وبدون اسم الخلاصة
    - الصورة تُرفع وتُرفق إن وُجدت
    """
    if not MASTODON_ACCESS_TOKEN:
        logging.warning("⚠️ Mastodon not configured (MASTODON_ACCESS_TOKEN missing). Skipping post.")
        return False

    text = re.sub(r'(?<!\w)@[\w\-]+', '', text).strip()

    if len(text) > MASTODON_CHAR_LIMIT:
        logging.error(f"❌ Text exceeds {MASTODON_CHAR_LIMIT} chars ({len(text)}). Skipping.")
        return False

    logging.info(f"📏 Mastodon text: {len(text)}/{MASTODON_CHAR_LIMIT} chars")

    headers = {"Authorization": f"Bearer {MASTODON_ACCESS_TOKEN}"}
    data = {
        "status": text,
        "visibility": "direct",
    }

    if image_url:
        media_id = upload_media_to_mastodon(image_url)
        if media_id:
            data["media_ids[]"] = [media_id]
        else:
            logging.warning("⚠️ Image upload failed. Posting text-only.")

    try:
        r = requests.post(f"{MASTODON_API_BASE_URL}/api/v1/statuses",
                          headers=headers, data=data, timeout=20)
        if r.status_code == 200:
            logging.info("✅ Posted to Mastodon (direct, with image if any).")
            return True
        logging.error(f"❌ Failed to post to Mastodon: {r.status_code} - {r.text}")
        return False
    except Exception as e:
        logging.error(f"❌ Failed to post to Mastodon: {e}")
        return False

# ===================== RSS XML =====================

def create_rss_xml(feed_name, entries):
    if MERGE_FEEDS:
        xml_file = os.path.join(RSS_DIR, "merged.xml")
    else:
        xml_file = os.path.join(RSS_DIR, f"{feed_name}.xml")

    entries = entries[-MAX_POSTS:]

    rss = ET.Element('rss')
    rss.set('version', '2.0')

    channel = ET.SubElement(rss, 'channel')

    if MERGE_FEEDS:
        ET.SubElement(channel, 'title').text = "All News"
        ET.SubElement(channel, 'description').text = f"All news from {len(RSS_FEEDS)} sources"
    else:
        ET.SubElement(channel, 'title').text = f"{feed_name} - Processed Feed"
        ET.SubElement(channel, 'description').text = f"Processed RSS feed from {feed_name}"

    ET.SubElement(channel, 'link').text = "https://github.com/bidjadraft/rss-translator"
    ET.SubElement(channel, 'language').text = LANGUAGE
    ET.SubElement(channel, 'lastBuildDate').text = datetime.now().strftime('%a, %d %b %Y %H:%M:%S GMT')

    for entry in entries:
        item = ET.SubElement(channel, 'item')

        title = entry.get('translated_title') or entry.get('title') or "News"
        ET.SubElement(item, 'title').text = title

        link = entry.get('link', '')
        ET.SubElement(item, 'link').text = link

        pub_date = entry.get('published') or datetime.now().strftime('%a, %d %b %Y %H:%M:%S GMT')
        ET.SubElement(item, 'pubDate').text = pub_date

        if entry.get('feed_source'):
            ET.SubElement(item, 'source').text = entry['feed_source']

        description = entry.get('processed_text', '')

        source_name = entry.get('feed_source', '')
        if link:
            source_line = f'<br><br>المصدر: <a href="{link}">{source_name or link}</a>'
        else:
            source_line = f'<br><br>المصدر: {source_name}'

        ET.SubElement(item, 'description').text = description + source_line

        if entry.get('image_url'):
            ET.SubElement(item, 'enclosure', {
                'url': entry['image_url'],
                'type': 'image/jpeg'
            })

    xml_str = ET.tostring(rss, encoding='unicode')
    pretty_xml = minidom.parseString(xml_str).toprettyxml(indent='  ', encoding='utf-8')

    with open(xml_file, 'wb') as f:
        f.write(pretty_xml)

    logging.info(f"📄 Created RSS XML with {len(entries)} entries: {xml_file}")
    return xml_file

# ===================== المعالجة الرئيسية =====================

def process_feed(feed_url):
    try:
        logging.info(f"{'='*60}")
        logging.info(f"🔄 Processing feed: {feed_url}")

        feed = feedparser.parse(feed_url)

        if not feed.entries:
            logging.warning(f"⚠️ No entries found in {feed_url}")
            return

        feed_name = extract_feed_name(feed_url, feed)
        logging.info(f"📛 Feed name: {feed_name}")

        tracker_data = load_tracker()
        last_id = tracker_data.get(feed_name, "")
        logging.info(f"📌 Last processed ID for {feed_name}: '{last_id}'")

        entries_sorted = sorted(feed.entries,
                                key=lambda e: e.get('published_parsed') or e.get('updated_parsed') or (0,))

        existing_entries = load_existing_entries(feed_name)
        new_entries_to_process = []
        processed_count = 0
        skipped_count = 0

        if not last_id:
            logging.info(f"🆕 First time processing '{feed_name}'. Processing latest post only.")
            new_entries_to_process = [entries_sorted[-1]]
        else:
            last_index = -1
            for i, entry in enumerate(entries_sorted):
                current_id = normalize_url(entry.get('guid') or entry.get('id') or entry.get('link'))
                if current_id == last_id:
                    last_index = i
                    break

            if last_index >= 0:
                new_entries_to_process = entries_sorted[last_index + 1:]
                logging.info(f"✨ Found {len(new_entries_to_process)} new posts in {feed_name}")
            else:
                logging.warning("⚠️ Last ID not found. Processing latest post only.")
                new_entries_to_process = [entries_sorted[-1]]

        if new_entries_to_process:
            for entry in new_entries_to_process:
                try:
                    post_id = normalize_url(entry.get('guid') or entry.get('id') or entry.get('link'))
                    post_url = entry.get('link', '')

                    logging.info(f"🎯 Processing post: {post_id}")

                    desc = entry.get('summary', '') or entry.get('description', '')
                    desc_text = clean_html(desc)

                    model_switcher = OllamaModelSwitcher(OLLAMA_MODELS)
                    processed_text = process_with_ollama(desc_text, model_switcher)

                    if not processed_text:
                        logging.warning(f"⚠️ AI processing failed for: {post_id}. Skipping.")
                        skipped_count += 1
                        continue

                    title_switcher = OllamaModelSwitcher(OLLAMA_MODELS)
                    translated_title = translate_title(entry.get('title', 'No Title'), title_switcher)

                    image_url = None
                    media_content = entry.get('media_content', [])
                    if media_content:
                        image_url = media_content[0].get('url', '')

                    if not image_url and post_url:
                        image_url = extract_image_from_url(post_url)

                    # ===== النشر إلى Mastodon: النص فقط (بلا مصدر وبلا اسم خلاصة) مع الصورة =====
                    post_to_mastodon(processed_text, image_url)

                    # ===== النشر إلى Substack: فقط لخلاصة alternativeto، مع رابط المصدر =====
                    if "alternativeto" in feed_url:
                        publish_note(
                            processed_text,
                            source_url=post_url,
                            source_name="المصدر: AlternativeTo",
                        )

                    processed_entry = {
                        'title': entry.get('title', 'No Title'),
                        'translated_title': translated_title,
                        'link': post_url,
                        'published': entry.get('published', ''),
                        'processed_text': processed_text,
                        'image_url': image_url,
                        'feed_source': feed_name
                    }

                    existing_entries.append(processed_entry)
                    tracker_data[feed_name] = post_id
                    processed_count += 1

                    logging.info(f"✅ Successfully processed: {entry.get('title', 'No Title')[:50]}...")

                except Exception as e:
                    logging.error(f"❌ Failed to process individual post: {e}")
                    skipped_count += 1
                    continue

        if existing_entries:
            existing_entries.sort(key=lambda e: e.get('published', ''), reverse=True)
            create_rss_xml(feed_name, existing_entries)
            save_tracker(tracker_data)
            logging.info(f"📊 Processed: {processed_count} | Skipped: {skipped_count} "
                         f"| Total in XML: {min(len(existing_entries), MAX_POSTS)}")
        else:
            logging.info(f"📭 No entries to save for {feed_name}")

    except Exception as e:
        logging.error(f"❌ Failed to process feed {feed_url}: {e}")

def main():
    logging.info("🚀 Starting Apps Bot with Ollama…")
    logging.info(f"📋 Models: {OLLAMA_MODELS}")
    logging.info(f"📡 Feeds: {len(RSS_FEEDS)} | 🌐 Language: {LANGUAGE} | 📝 Type: {CONTENT_TYPE}")

    for feed_url in RSS_FEEDS:
        process_feed(feed_url)
        time.sleep(2)

    logging.info(f"{'='*60}")
    logging.info("🎉 All feeds processed successfully!")
    logging.info(f"📄 RSS files saved in: {RSS_DIR}")
    logging.info(f"💾 Tracker file: {TRACKER_FILE}")

if __name__ == "__main__":
    main()

import os
import json
import asyncio
import requests
import re
import shutil
from urllib.parse import quote_plus
from datetime import datetime
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = os.environ.get('TELEGRAM_API_ID')
API_HASH = os.environ.get('TELEGRAM_API_HASH')
SESSION_STRING = os.environ.get('TELEGRAM_SESSION_STRING')
PUBLISH_CHANNEL_ID = -1002682088604
GIT_API_TOKEN = os.environ.get('GIT_API_TOKEN')

STATE_DIR = "./state"
CACHE_DIR = os.path.expanduser("~/.cache/ksu-manager")
MODULES_FILE_SRC = "./modules.json"
STATE_FILE = os.path.join(STATE_DIR, "state.json")

class StateManager:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        print(f"[INFO] State directory set to '{self.state_dir}'.")
        os.makedirs(self.state_dir, exist_ok=True)

    def load_state(self):
        return self.load_json(STATE_FILE, {"manifest": {}, "telegram_state": {}})

    def save_state(self, state):
        self.save_json(STATE_FILE, state)

    def load_json(self, path, default={}):
        print(f"[INFO] Reading JSON: {path}")
        if not os.path.exists(path):
            print(f"[WARNING] File not found: {path}. Returning default value.")
            return default
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[ERROR] JSON read error: {e}")
            return default

    def save_json(self, path, data):
        print(f"[INFO] Saving JSON: {path}")
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, sort_keys=True)
        except Exception as e:
            print(f"[ERROR] JSON save error: {e}")

class ModuleHandler:
    def __init__(self, client, state_manager):
        self.client = client
        self.state_manager = state_manager
        state = self.state_manager.load_state()
        self.manifest = state["manifest"]
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _get_api_call(self, url, is_json=True):
        headers = {"Authorization": f"Bearer {GIT_API_TOKEN}"} if "api.github.com" in url else {}
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json() if is_json else response.text
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] API call failed: {url} - {e}")
            return None

    async def _get_telegram_remote_info(self, module):
        channel = module['source_channel']
        keyword = module['source']
        try:
            async for message in self.client.iter_messages(channel, limit=100):
                if message.document and hasattr(message.document.attributes[0], 'file_name') and keyword.lower() in message.document.attributes[0].file_name.lower():
                    return {
                        'file_name': message.document.attributes[0].file_name,
                        'version_id': str(message.id),
                        'source_url': f"https://t.me/{message.chat.username}/{message.id}",
                        'date': message.date.strftime("%d.%m.%Y %H:%M"),
                        'telegram_message': message
                    }
            print(f"[INFO] No file found on Telegram for '{keyword}'.")
            return None
        except Exception as e:
            print(f"[ERROR] Error processing Telegram channel @{channel}: {e}")
            return None

    def _get_github_release_remote_info(self, module):
        url = f"https://api.github.com/repos/{module['source']}/releases/latest"
        data = self._get_api_call(url)
        if not isinstance(data, dict) or 'assets' not in data:
            print(f"[INFO] No file found on GitHub for '{module['source']}'.")
            return None
        asset = next((a for a in data['assets'] if re.search(module['asset_filter'], a['name'])), None)
        if asset:
            return {
                'file_name': asset['name'],
                'version_id': asset['updated_at'],
                'source_url': data.get('html_url', '#'),
                'date': datetime.strptime(asset['updated_at'], "%Y-%m-%dT%H:%M:%SZ").strftime("%d.%m.%Y %H:%M"),
                'download_url': asset['browser_download_url']
            }
        return None

    def _get_github_ci_remote_info(self, module):
        content = self._get_api_call(module['source'], is_json=False)
        if not content or not isinstance(content, str):
            print(f"[INFO] No file found on GitHub CI for '{module['source']}'.")
            return None
        match = re.search(r'https://nightly\.link/[^"]*\.zip', content)
        if match:
            url = match.group(0)
            filename = os.path.basename(url)
            return {
                'file_name': filename,
                'version_id': filename,
                'source_url': module['source'],
                'date': datetime.now().strftime("%d.%m.%Y %H:%M"),
                'download_url': url
            }
        return None

    def _get_gitlab_release_remote_info(self, module):
        url = f"https://gitlab.com/api/v4/projects/{quote_plus(module['source'])}/releases"
        data = self._get_api_call(url)
        if not isinstance(data, list) or not data:
            print(f"[INFO] No file found on GitLab for '{module['source']}'.")
            return None
        release = data[0]
        link = next((l for l in release.get('assets', {}).get('links', []) if re.search(module['asset_filter'], l['name'])), None)
        if link:
            return {
                'file_name': link['name'],
                'version_id': release['released_at'],
                'source_url': release.get('_links', {}).get('self', '#'),
                'date': datetime.strptime(release['released_at'], "%Y-%m-%dT%H:%M:%S.%f%z").strftime("%d.%m.%Y %H:%M"),
                'download_url': link['url']
            }
        return None

    def _download_file_sync(self, url, path):
        print(f"   -> Downloading: {url}")
        try:
            with requests.get(url, stream=True, timeout=180) as r:
                r.raise_for_status()
                with open(path, 'wb') as f:
                    shutil.copyfileobj(r.raw, f)
            return True
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Failed to download file: {url} - {e}")
            return False

    async def process_modules(self):
        print("\n--- Module Check and Download Phase Started ---")
        try:
            with open(MODULES_FILE_SRC, 'r', encoding='utf-8') as f:
                modules = json.load(f).get('modules', [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[CRITICAL MISTAKE] '{MODULES_FILE_SRC}' file not found or corrupted. Exiting: {e}")
            return

        state = self.state_manager.load_state()
        manifest_was_updated = False

        for module in sorted([m for m in modules if m.get('enabled')], key=lambda x: x['name']):
            name, type_ = module['name'], module['type']
            print(f"\n[PROCESS] Checking remote version: {name} (Type: {type_})")

            getter_func = {
                'telegram_forwarder': self._get_telegram_remote_info,
                'github_release': self._get_github_release_remote_info,
                'github_ci': self._get_github_ci_remote_info,
                'gitlab_release': self._get_gitlab_release_remote_info,
            }.get(type_)

            if not getter_func:
                print(f"[WARNING] Unsupported module type: {type_}. Skipping.")
                continue

            remote_info = await getter_func(module) if asyncio.iscoroutinefunction(getter_func) else getter_func(module)

            if not remote_info:
                print(f"[INFO] No file found in the source for '{name}'.")
                continue

            remote_version_id = remote_info['version_id']
            posted_version_id = state["manifest"].get(name, {}).get('version_id')

            if remote_version_id == posted_version_id:
                print(f"[INFO] '{name}' is already up-to-date (Version ID: {posted_version_id}). Skipping download.")
                continue

            print(f"[DOWNLOAD] New version of '{name}' will be downloaded (Cloud ID: {remote_version_id}, Channel ID: {posted_version_id or 'NONE'})")

            path = os.path.join(CACHE_DIR, remote_info['file_name'])
            success = False

            if 'telegram_message' in remote_info:
                message_to_download = remote_info.pop('telegram_message')
                downloaded_path = await self.client.download_media(message_to_download, path)
                success = downloaded_path is not None
            elif 'download_url' in remote_info:
                success = self._download_file_sync(remote_info.pop('download_url'), path)

            if success:
                manifest_was_updated = True
                old_file_in_manifest = state["manifest"].get(name, {}).get('file_name')
                if old_file_in_manifest and old_file_in_manifest != remote_info['file_name'] and os.path.exists(os.path.join(CACHE_DIR, old_file_in_manifest)):
                    os.remove(os.path.join(CACHE_DIR, old_file_in_manifest))

                state["manifest"][name] = remote_info
                print(f"[SUCCESSFUL] '{name}' downloaded and manifest updated.")
            else:
                print(f"[ERROR] '{name}' could not be downloaded, skipping in this cycle.")

        if manifest_was_updated:
            self.state_manager.save_state(state)
        else:
            print("\n[INFO] No modules were downloaded, manifest file unchanged.")

        print("--- Module Check and Download Phase Completed ---")

class TelethonPublisher:
    def __init__(self, client, state_manager):
        self.client = client
        self.state_manager = state_manager
        state = self.state_manager.load_state()
        self.manifest = state["manifest"]
        self.telegram_state = state["telegram_state"]

        try:
            with open(MODULES_FILE_SRC, 'r', encoding='utf-8') as f:
                modules_list = json.load(f).get('modules', [])
            self.modules_map = {m['name']: m for m in modules_list}
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[ERROR] Error reading '{MODULES_FILE_SRC}' file: {e}")
            self.modules_map = {}

    async def publish_updates(self):
        print("\n--- Telegram Publishing Phase Started ---")
        if not self.manifest:
            print("[INFO] Manifest is empty. Nothing to publish.")
            return

        state = self.state_manager.load_state()

        for name, info in sorted(self.manifest.items()):
            print(f"\n[PROCESS] Checking publish status: {name}")
            current_version_id = info.get('version_id')
            if not current_version_id:
                print(f"[WARNING] No version_id found in manifest for '{name}'. Skipping.")
                continue

            posted_version_id = state["telegram_state"].get(name, {}).get('version_id')

            if current_version_id == posted_version_id:
                print(f"[INFO] '{name}' is already up-to-date on Telegram.")
                continue

            current_filename = info['file_name']
            print(f"[UPDATE] New version of '{name}' will be published: {current_filename}")

            filepath = os.path.join(CACHE_DIR, current_filename)
            if not os.path.exists(filepath):
                print(f"[ERROR] File not found on disk: {filepath}. Skipping.")
                continue

            posted_info = state["telegram_state"].get(name)
            if posted_info and 'message_id' in posted_info:
                print(f"[TELEGRAM] Deleting old message (ID: {posted_info['message_id']})...")
                try:
                    await self.client.delete_messages(PUBLISH_CHANNEL_ID, posted_info['message_id'])
                except Exception as e:
                    print(f"[WARNING] Failed to delete old message: {e}")

            module_def = self.modules_map.get(name, {})
            display_name = module_def.get('description') or info['file_name']
            caption = (
                f"✨ <b>{display_name}</b>\n\n"
                f"✶ <b>File Name:</b> <code>{info['file_name']}</code>\n"
                f"✷ <b>Update Date:</b> {info['date']}\n\n"
                f"✹ <b><a href='{info['source_url']}'>Source</a></b>\n\n"
                f"✦ <b><a href='https://github.com/MematiBas42/Cephanelik_Updater'>Sent With MematiBas42/Cephanelik_Updater</a></b>\n"
            )

            print(f"[TELEGRAM] Uploading new file '{current_filename}'...")
            try:
                message = await self.client.send_file(
                    PUBLISH_CHANNEL_ID, filepath, caption=caption, parse_mode='html', silent=True)
                state["telegram_state"][name] = {
                    'message_id': message.id,
                    'file_name': current_filename,
                    'version_id': current_version_id
                }
                print(f"[SUCCESSFUL] '{name}' updated. New Message ID: {message.id}")
            except Exception as e:
                print(f"[CRITICAL MISTAKE] Failed to upload file: {name} - {e}")

        self.state_manager.save_state(state)
        print("--- Telegram Publishing Phase Completed ---")

async def main():
    print("==============================================")
    print(f"   Cephanelik Updater v7.0 Started")
    print(f"   {datetime.now()}")
    print("==============================================")

    if not all([API_ID, API_HASH, SESSION_STRING, GIT_API_TOKEN]):
        raise ValueError("[ERROR] All required environment variables (Secrets) must be set.")

    state_manager = StateManager(STATE_DIR)
    async with TelegramClient(StringSession(SESSION_STRING), int(API_ID), API_HASH) as client:
        handler = ModuleHandler(client, state_manager)
        await handler.process_modules()
        publisher = TelethonPublisher(client, state_manager)
        await publisher.publish_updates()

    print("\n[INFO] All operations completed successfully.")

if __name__ == "__main__":
    asyncio.run(main())

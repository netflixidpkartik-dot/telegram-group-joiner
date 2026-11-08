import json
import os
import threading
import time

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "joiner_state.json")
_lock = threading.RLock()

class Storage:
    def __init__(self, filepath=DATA_FILE):
        self.filepath = filepath
        self.data = {
            "queue": [],
            "processed": [],
            "joined_groups": {},   # str(chat_id): {"id": int, "title": str, "username": str, "access_hash": int}
            "failed_groups": [],   # [{"link": str, "error": str, "time": str}]
            "muted_count": 0,
            "combined_folder_link": "",
            "chatlist_slug": "",
            "filter_id": 2,
            "status_text": "Starting up...",
            "is_running": True
        }
        self.load()

    def load(self):
        with _lock:
            if os.path.exists(self.filepath):
                try:
                    with open(self.filepath, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        self.data.update(loaded)
                except Exception as e:
                    print(f"Error loading {self.filepath}: {e}")

    def save(self):
        with _lock:
            try:
                with open(self.filepath, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"Error saving {self.filepath}: {e}")

    def add_to_queue(self, links):
        added = 0
        with _lock:
            for l in links:
                l_clean = str(l).strip()
                if l_clean and l_clean not in self.data["processed"] and l_clean not in self.data["queue"]:
                    self.data["queue"].append(l_clean)
                    added += 1
        if added > 0:
            self.save()
        return added

    def requeue_failed(self, links=None):
        """Removes links from processed and failed_groups, re-adding them to queue"""
        added = 0
        with _lock:
            if links is None:
                # Requeue all from failed_groups
                failed_items = [f.get("link") for f in self.data.get("failed_groups", []) if f.get("link")]
                to_requeue = list(dict.fromkeys(failed_items))
                self.data["failed_groups"] = []
            else:
                to_requeue = [str(l).strip() for l in links if str(l).strip()]
                # Remove matched from failed_groups
                req_set = set(to_requeue)
                self.data["failed_groups"] = [f for f in self.data["failed_groups"] if f.get("link") not in req_set]

            for l in to_requeue:
                while l in self.data["processed"]:
                    self.data["processed"].remove(l)
                if l not in self.data["queue"]:
                    self.data["queue"].append(l)
                    added += 1
            if added > 0 or links is None:
                self.save()
        return added

    def pop_queue(self):
        with _lock:
            if self.data["queue"]:
                item = self.data["queue"].pop(0)
                self.data["processed"].append(item)
                self.save()
                return item
            return None

    def record_joined(self, chat_id, title, username=None, access_hash=None):
        with _lock:
            self.data["joined_groups"][str(chat_id)] = {
                "id": chat_id,
                "title": title or f"Group_{chat_id}",
                "username": username or "",
                "access_hash": access_hash,
                "joined_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            # Clean from failed if present
            if username:
                u_str = f"@{username}".lower()
                self.data["failed_groups"] = [
                    f for f in self.data["failed_groups"]
                    if u_str not in f.get("link", "").lower()
                ]
            self.save()

    def remove_restricted_groups(self, group_ids):
        with _lock:
            changed = False
            for gid in group_ids:
                s_gid = str(gid)
                if s_gid in self.data["joined_groups"]:
                    del self.data["joined_groups"][s_gid]
                    changed = True
            if changed:
                self.save()

    def record_failed(self, link, error):
        with _lock:
            self.data["failed_groups"].append({
                "link": link,
                "error": str(error),
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            })
            self.save()

    def set_folder_link(self, link, slug=""):
        with _lock:
            self.data["combined_folder_link"] = link
            if slug:
                self.data["chatlist_slug"] = slug
            self.save()

    def set_status(self, text):
        with _lock:
            self.data["status_text"] = text
            self.save()

    def get_stats(self):
        with _lock:
            return {
                "queue_len": len(self.data["queue"]),
                "joined_count": len(self.data["joined_groups"]),
                "failed_count": len(self.data["failed_groups"]),
                "combined_folder_link": self.data.get("combined_folder_link", ""),
                "status_text": self.data.get("status_text", "Idle"),
                "is_running": self.data.get("is_running", True)
            }

storage = Storage()

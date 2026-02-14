from flask import Flask, render_template, request, jsonify
from auth import Auth
from api import LocketAPI
import json
import time
import requests
import queue
import threading
import uuid
from datetime import datetime
import dotenv
import os

app = Flask(__name__)

dotenv.load_dotenv()

# Initialize API and Auth
subscription_ids = [
    "locket_1600_1y",
    "locket_199_1m",
    "locket_199_1m_only",
    "locket_3600_1y",
    "locket_399_1m_only",
]

auth = Auth(os.getenv("EMAIL"), os.getenv("PASSWORD"))
try:
    token = auth.get_token()
    api = LocketAPI(token)
except Exception as e:
    print(f"Error initializing API: {e}")
    api = None

# Queue Management System
class QueueManager:
    def __init__(self):
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.client_requests = {}
        self.processing_times = []
        self.current_processing = None
        self.worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self.worker_thread.start()
        print("Queue manager initialized and worker thread started")

    def add_to_queue(self, username):
        client_id = str(uuid.uuid4())
        request_data = {
            "username": username,
            "status": "waiting",
            "result": None,
            "error": None,
            "added_at": datetime.now(),
            "started_at": None,
            "completed_at": None,
        }
        with self.lock:
            self.client_requests[client_id] = request_data
        self.queue.put(client_id)
        return client_id

    def get_status(self, client_id):
        with self.lock:
            if client_id not in self.client_requests:
                return None
            request_data = self.client_requests[client_id].copy()
            position = self._get_position(client_id)
            total_queue = self.queue.qsize()
            if self.current_processing and self.current_processing != client_id:
                total_queue += 1
            estimated_time = self._estimate_wait_time(position)
            return {
                "client_id": client_id,
                "status": request_data["status"],
                "position": position,
                "total_queue": total_queue,
                "estimated_time": estimated_time,
                "result": request_data["result"],
                "error": request_data["error"],
            }

    def _get_position(self, client_id):
        if self.current_processing == client_id:
            return 0
        queue_list = list(self.queue.queue)
        if client_id in queue_list:
            return queue_list.index(client_id) + 1
        return 0

    def _estimate_wait_time(self, position):
        if position <= 0: return 0
        avg_time = 5
        if self.processing_times:
            avg_time = sum(self.processing_times[-10:]) / len(self.processing_times[-10:])
        return int(position * avg_time)

    def _process_queue(self):
        while True:
            try:
                client_id = self.queue.get(timeout=1)
                with self.lock:
                    if client_id not in self.client_requests:
                        continue
                    self.current_processing = client_id
                    self.client_requests[client_id]["status"] = "processing"
                    self.client_requests[client_id]["started_at"] = datetime.now()
                self._process_request(client_id)
                with self.lock:
                    self.current_processing = None
                    if client_id in self.client_requests:
                        self.client_requests[client_id]["completed_at"] = datetime.now()
                        started = self.client_requests[client_id]["started_at"]
                        completed = self.client_requests[client_id]["completed_at"]
                        duration = (completed - started).total_seconds()
                        self.processing_times.append(duration)
                        if len(self.processing_times) > 20:
                            self.processing_times.pop(0)
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Error in queue processing: {e}")
                with self.lock:
                    self.current_processing = None

    def _process_request(self, client_id):
        username = "Unknown"
        try:
            with self.lock:
                username = self.client_requests[client_id]["username"]
            
            account_info = api.getUserByUsername(username)
            if not account_info or "result" not in account_info:
                raise Exception("User not found or API error")
            
            user_data = account_info.get("result", {}).get("data")
            uid_target = user_data.get("uid")
            restore_result = api.restorePurchase(uid_target)
            
            entitlements = restore_result.get("subscriber", {}).get("entitlements", {})
            gold_entitlement = entitlements.get("Gold", {})

            if gold_entitlement.get("product_identifier") in subscription_ids:
                send_telegram_notification(username, uid_target, gold_entitlement.get("product_identifier"), restore_result)
                with self.lock:
                    self.client_requests[client_id]["status"] = "completed"
                    self.client_requests[client_id]["result"] = {"success": True, "msg": f"Success for {username}!"}
            else:
                raise Exception("Gold entitlement not found.")
        except Exception as e:
            with self.lock:
                self.client_requests[client_id]["status"] = "error"
                self.client_requests[client_id]["error"] = str(e)

queue_manager = QueueManager()

def refresh_api_token():
    global api
    try:
        new_token = auth.create_token()
        api = LocketAPI(new_token)
        return True
    except:
        return False

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/get-user-info", methods=["POST"])
def get_user_info():
    if not api:
        return jsonify({"success": False, "msg": "API not initialized."}), 500
    data = request.json
    username = data.get("username")
    if not username:
        return jsonify({"success": False, "msg": "Username required"}), 400
    try:
        account_info = api.getUserByUsername(username)
        user_data = account_info.get("result", {}).get("data")
        return jsonify({"success": True, "data": {
            "uid": user_data.get("uid"),
            "username": user_data.get("username"),
            "first_name": user_data.get("first_name", ""),
            "profile_picture_url": user_data.get("profile_picture_url", "")
        }})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 500

def send_telegram_notification(username, uid, product_id, raw_json):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id: return
    message = f"✅ Locket Gold Unlocked!\nUser: {username}\nProduct: {product_id}"
    requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json={"chat_id": chat_id, "text": message})

@app.route("/api/restore", methods=["POST"])
def restore_purchase():
    if not api: return jsonify({"success": False, "msg": "API not initialized."}), 500
    username = request.json.get("username")
    client_id = queue_manager.add_to_queue(username)
    status = queue_manager.get_status(client_id)
    return jsonify({"success": True, "client_id": client_id, "position": status["position"]})

@app.route("/api/queue/status", methods=["POST"])
def queue_status():
    client_id = request.json.get("client_id")
    status = queue_manager.get_status(client_id)
    return jsonify({"success": True, **status}) if status else (jsonify({"success": False}), 404)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

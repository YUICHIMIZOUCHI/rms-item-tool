# -*- coding: utf-8 -*-
"""RMS商品一括編集ツール（ローカルWebアプリ）"""
import copy
import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser

from flask import Flask, jsonify, request, send_file, render_template

from rms_client import RmsClient, MockRmsClient, RmsError

if getattr(sys, "frozen", False):
    # PyInstaller製EXEとして実行中：テンプレートは展開先、データはEXEの隣に置く
    RESOURCE_DIR = sys._MEIPASS
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    RESOURCE_DIR = BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "items.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

APP_VERSION = "1.0.3"

app = Flask(__name__, template_folder=os.path.join(RESOURCE_DIR, "templates"))
app.config["JSON_AS_ASCII"] = False


@app.after_request
def no_cache(resp):
    # 古い画面がブラウザにキャッシュされて新機能が見えなくなるのを防ぐ
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

# ----------------- 設定 -----------------
DEFAULT_CONFIG = {"serviceSecret": "", "licenseKey": "", "demoMode": True, "waitMs": 700}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


_mock = None


def get_client():
    global _mock
    cfg = load_config()
    if cfg.get("demoMode"):
        if _mock is None:
            _mock = MockRmsClient()
        return _mock
    if not cfg.get("serviceSecret") or not cfg.get("licenseKey"):
        raise RmsError(0, "APIキーが未設定です。[設定]タブで serviceSecret / licenseKey を登録してください。")
    return RmsClient(cfg["serviceSecret"], cfg["licenseKey"], wait_ms=int(cfg.get("waitMs", 700)))


# ----------------- DB -----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items(
                manage_number TEXT PRIMARY KEY,
                title TEXT, tagline TEXT, item_number TEXT,
                hide_item INTEGER DEFAULT 0,
                min_price REAL, max_price REAL,
                raw TEXT, edited TEXT,
                is_new INTEGER DEFAULT 0,
                downloaded_at TEXT, updated_local_at TEXT
            );
            CREATE TABLE IF NOT EXISTS logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, manage_number TEXT, action TEXT,
                status TEXT, message TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, run_at TEXT, targets TEXT, snapshot TEXT,
                restore_price INTEGER DEFAULT 1,
                period_mode TEXT DEFAULT 'none',
                status TEXT DEFAULT 'pending',
                result TEXT, created_at TEXT, executed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sale_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, targets TEXT, snapshot TEXT, created_at TEXT
            );
            """
        )
        # 既存DBへの列追加（セール対象フラグ）
        try:
            conn.execute("ALTER TABLE items ADD COLUMN sale_flag INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def add_log(manage_number, action, status, message=""):
    with db() as conn:
        conn.execute(
            "INSERT INTO logs(ts,manage_number,action,status,message) VALUES(?,?,?,?,?)",
            (now(), manage_number, action, status, str(message)[:2000]),
        )


def item_summary(item):
    prices = []
    for v in (item.get("variants") or {}).values():
        if isinstance(v, dict) and isinstance(v.get("standardPrice"), (int, float)):
            prices.append(v["standardPrice"])
    return {
        "title": item.get("title", ""),
        "tagline": item.get("tagline", ""),
        "item_number": item.get("itemNumber", ""),
        "hide_item": 1 if item.get("hideItem") else 0,
        "min_price": min(prices) if prices else None,
        "max_price": max(prices) if prices else None,
    }


def save_downloaded(item):
    mn = item.get("manageNumber")
    if not mn:
        return
    s = item_summary(item)
    with db() as conn:
        conn.execute(
            """INSERT INTO items(manage_number,title,tagline,item_number,hide_item,min_price,max_price,raw,edited,is_new,downloaded_at)
               VALUES(?,?,?,?,?,?,?,?,NULL,0,?)
               ON CONFLICT(manage_number) DO UPDATE SET
                 title=excluded.title, tagline=excluded.tagline, item_number=excluded.item_number,
                 hide_item=excluded.hide_item, min_price=excluded.min_price, max_price=excluded.max_price,
                 raw=excluded.raw, edited=NULL, is_new=0, downloaded_at=excluded.downloaded_at""",
            (mn, s["title"], s["tagline"], s["item_number"], s["hide_item"],
             s["min_price"], s["max_price"], json.dumps(item, ensure_ascii=False), now()),
        )


def save_local_edit(mn, item, is_new=None):
    s = item_summary(item)
    with db() as conn:
        if is_new:
            conn.execute(
                """INSERT INTO items(manage_number,title,tagline,item_number,hide_item,min_price,max_price,raw,edited,is_new,updated_local_at)
                   VALUES(?,?,?,?,?,?,?,NULL,?,1,?)
                   ON CONFLICT(manage_number) DO UPDATE SET
                     title=excluded.title, tagline=excluded.tagline, item_number=excluded.item_number,
                     hide_item=excluded.hide_item, min_price=excluded.min_price, max_price=excluded.max_price,
                     edited=excluded.edited, is_new=1, updated_local_at=excluded.updated_local_at""",
                (mn, s["title"], s["tagline"], s["item_number"], s["hide_item"],
                 s["min_price"], s["max_price"], json.dumps(item, ensure_ascii=False), now()),
            )
        else:
            conn.execute(
                """UPDATE items SET title=?, tagline=?, item_number=?, hide_item=?, min_price=?, max_price=?,
                   edited=?, updated_local_at=? WHERE manage_number=?""",
                (s["title"], s["tagline"], s["item_number"], s["hide_item"],
                 s["min_price"], s["max_price"], json.dumps(item, ensure_ascii=False), now(), mn),
            )


def get_effective(row):
    """編集済があれば編集済、なければ元データ"""
    src = row["edited"] or row["raw"]
    return json.loads(src) if src else None


# ----------------- バックグラウンドジョブ -----------------
JOB = {"running": False, "kind": "", "done": 0, "total": None, "message": "", "errors": [], "finished": True}
_job_lock = threading.Lock()


def start_job(kind, target, *args):
    with _job_lock:
        if JOB["running"]:
            return False
        JOB.update({"running": True, "kind": kind, "done": 0, "total": None,
                    "message": "開始しました", "errors": [], "finished": False})
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    return True


def finish_job(message):
    JOB.update({"running": False, "finished": True, "message": message})


def job_download():
    try:
        client = get_client()
        count = 0

        def progress(fetched, total):
            JOB["done"] = fetched
            JOB["total"] = total
            JOB["message"] = f"{fetched}件 取得済み" + (f" / 全{total}件" if total else "")

        for item in client.iter_all_items(progress_cb=progress):
            save_downloaded(item)
            count += 1
        add_log("", "商品一覧ダウンロード", "OK", f"{count}件")
        finish_job(f"完了：{count}件をダウンロードしました")
    except Exception as e:
        add_log("", "商品一覧ダウンロード", "ERROR", str(e))
        JOB["errors"].append(str(e))
        finish_job(f"エラー：{e}")


PATCH_KEYS = [
    "itemNumber", "title", "tagline", "productDescription", "salesDescription",
    "genreId", "tags", "hideItem", "itemType", "images", "whiteBgImage",
    "payment", "features", "pointCampaign", "itemDisplaySequence",
    "purchasablePeriod", "releaseDate", "unlimitedInventoryFlag", "variants",
    "variantSelectors",
]


def diff_item(raw, edited):
    """トップレベル項目単位の差分を返す（PATCH用）"""
    patch = {}
    for k in PATCH_KEYS:
        if edited.get(k) != raw.get(k):
            patch[k] = edited.get(k)
    return patch


# RMSのGETレスポンスに含まれるが、登録・更新(PUT/PATCH)では送信できない読み取り専用フィールド
READONLY_FIELDS = {"created", "updated", "itemUrl"}
_UNRECOGNIZED_RE = re.compile(r'Unrecognized field "([^"]+)"')


def strip_keys(obj, keys):
    """辞書/リストを再帰的に走査し、指定キーを除去した新しいオブジェクトを返す"""
    if isinstance(obj, dict):
        return {k: strip_keys(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [strip_keys(v, keys) for v in obj]
    return obj


def send_with_retry(send_fn, body, max_retry=5):
    """未知の読み取り専用フィールドをエラーメッセージから特定して除去しつつ再送する"""
    body = strip_keys(body, READONLY_FIELDS)
    for _ in range(max_retry):
        try:
            return send_fn(body)
        except RmsError as e:
            m = _UNRECOGNIZED_RE.search(e.message or "")
            if e.status == 400 and m:
                body = strip_keys(body, {m.group(1)})
                continue
            raise
    return send_fn(body)


def job_apply(manage_numbers):
    try:
        client = get_client()
        with db() as conn:
            if manage_numbers:
                q = ",".join("?" * len(manage_numbers))
                rows = conn.execute(
                    f"SELECT * FROM items WHERE manage_number IN ({q}) AND (edited IS NOT NULL OR is_new=1)",
                    manage_numbers).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM items WHERE edited IS NOT NULL OR is_new=1").fetchall()
        JOB["total"] = len(rows)
        ok = ng = 0
        for row in rows:
            mn = row["manage_number"]
            try:
                edited = json.loads(row["edited"])
                quantity = edited.pop("_initialQuantity", None)
                edited.pop("manageNumber", None)
                if row["is_new"]:
                    send_with_retry(lambda b: client.upsert_item(mn, b), edited)
                    if quantity is not None:
                        invs = [{"manageNumber": mn, "variantId": vid,
                                 "mode": "ABSOLUTE", "quantity": int(quantity)}
                                for vid in (edited.get("variants") or {})]
                        if invs:
                            client.bulk_upsert_inventory(invs)
                    add_log(mn, "新規登録", "OK")
                else:
                    raw = json.loads(row["raw"])
                    patch = diff_item(raw, edited)
                    if patch:
                        send_with_retry(lambda b: client.patch_item(mn, b), patch)
                    add_log(mn, "更新", "OK", "変更項目: " + ", ".join(patch.keys()) if patch else "変更なし")
                edited["manageNumber"] = mn
                with db() as conn:
                    s = item_summary(edited)
                    conn.execute(
                        """UPDATE items SET raw=?, edited=NULL, is_new=0, title=?, tagline=?,
                           item_number=?, hide_item=?, min_price=?, max_price=? WHERE manage_number=?""",
                        (json.dumps(edited, ensure_ascii=False), s["title"], s["tagline"],
                         s["item_number"], s["hide_item"], s["min_price"], s["max_price"], mn),
                    )
                ok += 1
            except Exception as e:
                ng += 1
                JOB["errors"].append(f"{mn}: {e}")
                add_log(mn, "新規登録" if row["is_new"] else "更新", "ERROR", str(e))
            JOB["done"] += 1
            JOB["message"] = f"{JOB['done']}/{len(rows)}件 処理済み（成功{ok} 失敗{ng}）"
        finish_job(f"完了：成功{ok}件 / 失敗{ng}件")
    except Exception as e:
        JOB["errors"].append(str(e))
        finish_job(f"エラー：{e}")


# ----------------- 復元予約（スケジューラ） -----------------
OVERDUE_GRACE_MIN = 10  # これを超えて時刻を過ぎていた予約は自動実行せず「要確認」にする


def make_snapshot(targets):
    """対象商品の現在値（価格・二重価格・販売期間）を保存用に取得。(snapshot, missing) を返す"""
    snapshot = {}
    q = ",".join("?" * len(targets))
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM items WHERE manage_number IN ({q})", targets).fetchall()
    found = set()
    for row in rows:
        item = get_effective(row)
        if not item:
            continue
        found.add(row["manage_number"])
        prices = {}
        for vid, v in (item.get("variants") or {}).items():
            if isinstance(v, dict) and isinstance(v.get("standardPrice"), (int, float)):
                prices[vid] = {"standardPrice": v.get("standardPrice"),
                               "referencePrice": v.get("referencePrice")}
        snapshot[row["manage_number"]] = {
            "prices": prices,
            "purchasablePeriod": item.get("purchasablePeriod"),
        }
    missing = [t for t in targets if t not in found]
    return snapshot, missing


def _restore_variant(v, sv):
    """スナップショット値でSKUを復元。変更があればTrue"""
    changed = False
    if isinstance(sv, dict):
        sp = sv.get("standardPrice")
        if sp is not None and v.get("standardPrice") != sp:
            v["standardPrice"] = sp
            changed = True
        rp = sv.get("referencePrice")
        if rp is None:
            if "referencePrice" in v:
                v.pop("referencePrice")
                changed = True
        elif v.get("referencePrice") != rp:
            v["referencePrice"] = rp
            changed = True
    else:  # 旧形式（数値のみ）
        if v.get("standardPrice") != sv:
            v["standardPrice"] = sv
            changed = True
    return changed


def job_scheduled_restore(job_id):
    try:
        with db() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] != "running":
            finish_job("予約が見つからないか実行できない状態です")
            return
        client = get_client()
        targets = json.loads(job["targets"])
        snapshot = json.loads(job["snapshot"])
        restore_price = bool(job["restore_price"])
        period_mode = job["period_mode"]  # none / snapshot / clear
        JOB["total"] = len(targets)
        ok = ng = 0
        details = []
        for mn in targets:
            try:
                snap = snapshot.get(mn) or {}
                current = client.get_item(mn)
                patch = {}
                if restore_price and snap.get("prices"):
                    variants = current.get("variants") or {}
                    changed = False
                    for vid, sv in snap["prices"].items():
                        v = variants.get(vid)
                        if isinstance(v, dict) and _restore_variant(v, sv):
                            changed = True
                    if changed:
                        patch["variants"] = variants
                if period_mode == "snapshot":
                    if current.get("purchasablePeriod") != snap.get("purchasablePeriod"):
                        patch["purchasablePeriod"] = snap.get("purchasablePeriod")
                elif period_mode == "clear":
                    if current.get("purchasablePeriod") is not None:
                        patch["purchasablePeriod"] = None
                if patch:
                    send_with_retry(lambda b: client.patch_item(mn, b), patch)
                    # ローカルDBのrawにも反映して整合を保つ
                    with db() as conn:
                        row = conn.execute("SELECT raw FROM items WHERE manage_number=?", (mn,)).fetchone()
                    if row and row["raw"]:
                        item = json.loads(row["raw"])
                        for k, v in patch.items():
                            if v is None:
                                item.pop(k, None)
                            else:
                                item[k] = v
                        s = item_summary(item)
                        with db() as conn:
                            conn.execute(
                                """UPDATE items SET raw=?, title=?, tagline=?, item_number=?,
                                   hide_item=?, min_price=?, max_price=? WHERE manage_number=?""",
                                (json.dumps(item, ensure_ascii=False), s["title"], s["tagline"],
                                 s["item_number"], s["hide_item"], s["min_price"], s["max_price"], mn))
                    add_log(mn, "予約復元", "OK", "復元項目: " + ", ".join(patch.keys()))
                    details.append(f"{mn}: OK ({', '.join(patch.keys())})")
                else:
                    add_log(mn, "予約復元", "OK", "変更なし（すでに復元済み）")
                    details.append(f"{mn}: 変更なし")
                ok += 1
            except Exception as e:
                ng += 1
                add_log(mn, "予約復元", "ERROR", str(e))
                details.append(f"{mn}: ERROR {e}")
                JOB["errors"].append(f"{mn}: {e}")
            JOB["done"] += 1
            JOB["message"] = f"予約復元 {JOB['done']}/{len(targets)}件（成功{ok} 失敗{ng}）"
        status = "done" if ng == 0 else "error"
        with db() as conn:
            conn.execute("UPDATE jobs SET status=?, result=?, executed_at=? WHERE id=?",
                         (status, "\n".join(details)[:8000], now(), job_id))
        finish_job(f"予約復元 完了：成功{ok}件 / 失敗{ng}件")
    except Exception as e:
        with db() as conn:
            conn.execute("UPDATE jobs SET status='error', result=? WHERE id=?", (str(e)[:2000], job_id))
        JOB["errors"].append(str(e))
        finish_job(f"予約復元エラー：{e}")


def _try_start_scheduled(job_id, allowed_status=("pending",)):
    """予約をrunningにしてバックグラウンド実行。実行できなければFalse"""
    with db() as conn:
        ph = ",".join("?" * len(allowed_status))
        cur = conn.execute(
            f"UPDATE jobs SET status='running' WHERE id=? AND status IN ({ph})",
            (job_id, *allowed_status))
        if cur.rowcount == 0:
            return False
    if start_job("scheduled_restore", job_scheduled_restore, job_id):
        return True
    with db() as conn:
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (allowed_status[0], job_id))
    return False


def scheduler_loop():
    while True:
        time.sleep(20)
        try:
            now_s = time.strftime("%Y-%m-%dT%H:%M")
            with db() as conn:
                rows = conn.execute(
                    "SELECT id, run_at FROM jobs WHERE status='pending' AND run_at <= ?",
                    (now_s,)).fetchall()
            for r in rows:
                try:
                    overdue_min = (time.time() - time.mktime(
                        time.strptime(r["run_at"], "%Y-%m-%dT%H:%M"))) / 60.0
                except ValueError:
                    overdue_min = 0
                if overdue_min > OVERDUE_GRACE_MIN:
                    # 停止中に時刻を過ぎた予約は勝手に実行しない（要確認）
                    with db() as conn:
                        conn.execute(
                            "UPDATE jobs SET status='needs_confirm' WHERE id=? AND status='pending'",
                            (r["id"],))
                    add_log("", "予約復元", "WARN",
                            f"予約#{r['id']} は実行時刻を過ぎていたため保留にしました。[予約]タブから実行してください。")
                    continue
                _try_start_scheduled(r["id"])
        except Exception:
            pass


@app.route("/api/jobs", methods=["GET", "POST"])
def api_jobs():
    if request.method == "POST":
        data = request.json or {}
        name = (data.get("name") or "").strip() or "復元予約"
        run_at = (data.get("runAt") or "").strip()
        targets = data.get("targets") or []
        restore_price = 1 if data.get("restorePrice", True) else 0
        period_mode = data.get("periodMode", "none")
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", run_at):
            return jsonify({"message": "実行日時を指定してください"}), 400
        if run_at <= time.strftime("%Y-%m-%dT%H:%M"):
            return jsonify({"message": "実行日時は未来の日時を指定してください"}), 400
        if not targets:
            return jsonify({"message": "対象商品が選択されていません（商品一覧でチェックしてください）"}), 400
        if not restore_price and period_mode == "none":
            return jsonify({"message": "復元する内容（価格または販売期間）を選択してください"}), 400
        # 現在のローカル値（＝セール設定前の値）をスナップショット
        snapshot, missing = make_snapshot(targets)
        if missing:
            return jsonify({"message": f"ローカルにない商品が含まれています: {', '.join(missing[:5])}"}), 400
        with db() as conn:
            cur = conn.execute(
                """INSERT INTO jobs(name,run_at,targets,snapshot,restore_price,period_mode,status,created_at)
                   VALUES(?,?,?,?,?,?,'pending',?)""",
                (name, run_at, json.dumps(targets), json.dumps(snapshot, ensure_ascii=False),
                 restore_price, period_mode, now()))
            job_id = cur.lastrowid
        return jsonify({"ok": True, "id": job_id,
                        "message": f"予約#{job_id} を作成しました（{len(targets)}件・{run_at.replace('T',' ')} 実行）。"
                                   "現在の価格・販売期間を保存済み。このあとセール設定を反映してOKです。"})
    with db() as conn:
        rows = conn.execute("SELECT id,name,run_at,targets,restore_price,period_mode,status,result,"
                            "created_at,executed_at FROM jobs ORDER BY id DESC LIMIT 100").fetchall()
    jobs = []
    for r in rows:
        d = dict(r)
        d["targetCount"] = len(json.loads(r["targets"]))
        del d["targets"]
        jobs.append(d)
    return jsonify({"jobs": jobs})


@app.route("/api/jobs/<int:job_id>/cancel", methods=["POST"])
def api_job_cancel(job_id):
    with db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='canceled' WHERE id=? AND status IN ('pending','needs_confirm','error')",
            (job_id,))
    if cur.rowcount == 0:
        return jsonify({"message": "取消できない状態です（実行中または完了済み）"}), 400
    return jsonify({"ok": True, "message": f"予約#{job_id} を取り消しました"})


@app.route("/api/jobs/<int:job_id>/run", methods=["POST"])
def api_job_run(job_id):
    if _try_start_scheduled(job_id, allowed_status=("pending", "needs_confirm", "error")):
        return jsonify({"ok": True, "message": f"予約#{job_id} を実行開始しました"})
    return jsonify({"message": "実行できません（別の処理が実行中か、状態が不正です）"}), 409


# ----------------- セール専用機能 -----------------
@app.route("/api/sale/flag", methods=["POST"])
def api_sale_flag():
    """セール対象フラグの設定/解除"""
    data = request.json or {}
    targets = data.get("targets") or []
    value = 1 if data.get("value") else 0
    if data.get("clearAll"):
        with db() as conn:
            conn.execute("UPDATE items SET sale_flag=0")
        return jsonify({"ok": True, "message": "セール対象をすべて解除しました"})
    if not targets:
        return jsonify({"message": "対象商品がありません"}), 400
    q = ",".join("?" * len(targets))
    with db() as conn:
        conn.execute(f"UPDATE items SET sale_flag=? WHERE manage_number IN ({q})", [value] + targets)
    return jsonify({"ok": True,
                    "message": f"{len(targets)}件をセール対象に{'追加' if value else 'から解除'}しました"})


def _save_sale_history(name, targets, snapshot):
    """セール履歴を保存（最新2回分のみ保持）"""
    with db() as conn:
        conn.execute("INSERT INTO sale_history(name,targets,snapshot,created_at) VALUES(?,?,?,?)",
                     (name, json.dumps(targets), json.dumps(snapshot, ensure_ascii=False), now()))
        conn.execute("""DELETE FROM sale_history WHERE id NOT IN
                        (SELECT id FROM sale_history ORDER BY id DESC LIMIT 2)""")


@app.route("/api/sale/start", methods=["POST"])
def api_sale_start():
    """セール開始：履歴保存→セール設定をローカル適用→RMSに反映→自動復元予約を作成"""
    if JOB["running"]:
        return jsonify({"message": "別の処理が実行中です"}), 409
    data = request.json or {}
    name = (data.get("name") or "").strip() or "セール"
    with db() as conn:
        rows = conn.execute("SELECT manage_number FROM items WHERE sale_flag=1").fetchall()
    targets = [r["manage_number"] for r in rows]
    if not targets:
        return jsonify({"message": "セール対象商品がありません。商品一覧の［セール］列でチェックしてください"}), 400

    price_mode = data.get("priceMode", "")
    price_value = data.get("priceValue", "")
    period_start = (data.get("periodStart") or "").strip()
    period_end = (data.get("periodEnd") or "").strip()
    set_ref = bool(data.get("setRefPrice"))
    ref_type = data.get("refType", "2")
    auto_restore = bool(data.get("autoRestore", True))
    restore_at = (data.get("restoreAt") or "").strip()
    restore_period_mode = data.get("restorePeriodMode", "snapshot")

    if not price_mode and not period_start and not period_end and not set_ref:
        return jsonify({"message": "セール内容（価格・期間・二重価格のいずれか）を設定してください"}), 400
    if price_mode and price_value == "":
        return jsonify({"message": "価格の値を入力してください"}), 400
    if auto_restore:
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", restore_at):
            return jsonify({"message": "自動復元の日時を指定してください"}), 400
        if restore_at <= time.strftime("%Y-%m-%dT%H:%M"):
            return jsonify({"message": "自動復元の日時は未来を指定してください"}), 400

    # 1) 履歴保存（2回分保持）
    snapshot, missing = make_snapshot(targets)
    if missing:
        return jsonify({"message": f"ローカルにない商品が含まれています: {', '.join(missing[:5])}"}), 400
    _save_sale_history(name, targets, snapshot)

    # 2) セール設定をローカル適用
    q = ",".join("?" * len(targets))
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM items WHERE manage_number IN ({q})", targets).fetchall()
    changed = 0
    for row in rows:
        item = get_effective(row)
        if not item:
            continue
        before = json.dumps(item, ensure_ascii=False)
        for v in (item.get("variants") or {}).values():
            if not isinstance(v, dict) or not isinstance(v.get("standardPrice"), (int, float)):
                continue
            original = v["standardPrice"]
            if price_mode:
                value = float(price_value)
                if price_mode == "percent":
                    p = round(original * (1 + value / 100.0))
                elif price_mode == "delta":
                    p = original + value
                else:
                    p = value
                v["standardPrice"] = max(int(round(p)), 1)
            if set_ref:
                # 元の販売価格を二重価格（表示価格）として設定
                rp = v.get("referencePrice") or {}
                rp["displayType"] = rp.get("displayType") or "REFERENCE_PRICE"
                rp["type"] = int(ref_type) if str(ref_type).lstrip("-").isdigit() else ref_type
                rp["value"] = int(original)
                v["referencePrice"] = rp
        if period_start or period_end:
            period = {}
            if period_start:
                period["start"] = period_start
            if period_end:
                period["end"] = period_end
            item["purchasablePeriod"] = period
        if json.dumps(item, ensure_ascii=False) != before:
            save_local_edit(row["manage_number"], item, is_new=bool(row["is_new"]))
            changed += 1

    # 3) 自動復元予約を作成
    job_id = None
    if auto_restore:
        with db() as conn:
            cur = conn.execute(
                """INSERT INTO jobs(name,run_at,targets,snapshot,restore_price,period_mode,status,created_at)
                   VALUES(?,?,?,?,1,?,'pending',?)""",
                (f"{name} 自動戻し", restore_at, json.dumps(targets),
                 json.dumps(snapshot, ensure_ascii=False), restore_period_mode, now()))
            job_id = cur.lastrowid

    # 4) RMSに反映（バックグラウンド）
    started = start_job("apply", job_apply, targets)
    add_log("", "セール開始", "OK",
            f"{name}: 対象{len(targets)}件 変更{changed}件 復元予約#{job_id or 'なし'}")
    return jsonify({"ok": True, "jobId": job_id, "changed": changed, "targets": len(targets),
                    "applyStarted": started,
                    "message": f"セール設定を適用しました（対象{len(targets)}件）。"
                               + (f"RMSへの反映を開始しました。" if started else "反映は[RMSに反映]から実行してください。")
                               + (f" 自動復元予約#{job_id}（{restore_at.replace('T',' ')}）を作成済み。" if job_id else "")})


@app.route("/api/sale/history")
def api_sale_history():
    with db() as conn:
        rows = conn.execute("SELECT id,name,targets,created_at FROM sale_history ORDER BY id DESC").fetchall()
    return jsonify({"history": [
        {"id": r["id"], "name": r["name"], "created_at": r["created_at"],
         "targetCount": len(json.loads(r["targets"]))} for r in rows]})


@app.route("/api/sale/restore", methods=["POST"])
def api_sale_restore():
    """履歴からの復元：即時実行または日時指定で予約作成"""
    data = request.json or {}
    hid = data.get("historyId")
    when = data.get("when", "now")  # now / schedule
    run_at = (data.get("runAt") or "").strip()
    period_mode = data.get("periodMode", "snapshot")
    with db() as conn:
        h = conn.execute("SELECT * FROM sale_history WHERE id=?", (hid,)).fetchone()
    if not h:
        return jsonify({"message": "履歴が見つかりません"}), 404
    if when == "schedule":
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", run_at) or run_at <= time.strftime("%Y-%m-%dT%H:%M"):
            return jsonify({"message": "未来の日時を指定してください"}), 400
    else:
        run_at = time.strftime("%Y-%m-%dT%H:%M")
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO jobs(name,run_at,targets,snapshot,restore_price,period_mode,status,created_at)
               VALUES(?,?,?,?,1,?,'pending',?)""",
            (f"履歴復元: {h['name']}", run_at, h["targets"], h["snapshot"], period_mode, now()))
        job_id = cur.lastrowid
    if when == "now":
        if _try_start_scheduled(job_id):
            return jsonify({"ok": True, "message": f"履歴「{h['name']}」の値への復元を開始しました（予約#{job_id}）"})
        return jsonify({"message": "別の処理が実行中です。処理完了後に[予約]タブから実行してください"}), 409
    return jsonify({"ok": True, "message": f"復元予約#{job_id} を作成しました（{run_at.replace('T',' ')} 実行）"})


# ----------------- ルーティング -----------------
@app.route("/")
def index():
    return render_template("index.html", version=APP_VERSION)


@app.route("/favicon.ico")
def favicon():
    path = os.path.join(RESOURCE_DIR, "app.ico")
    if os.path.exists(path):
        return send_file(path, mimetype="image/x-icon")
    return "", 404


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """ブラウザの［終了］ボタンからツール本体を停止する。
    コンソール非表示のEXEでは、これが唯一の終了手段になる。"""
    def _die():
        time.sleep(0.4)   # レスポンスを返しきってから落とす
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        cfg = load_config()
        data = request.json or {}
        for k in ("serviceSecret", "licenseKey", "demoMode", "waitMs"):
            if k in data:
                cfg[k] = data[k]
        save_config(cfg)
    cfg = load_config()
    masked = dict(cfg)
    if masked.get("serviceSecret"):
        masked["serviceSecret"] = masked["serviceSecret"][:4] + "****"
    if masked.get("licenseKey"):
        masked["licenseKey"] = masked["licenseKey"][:4] + "****"
    masked["hasKeys"] = bool(cfg.get("serviceSecret") and cfg.get("licenseKey"))
    return jsonify(masked)


@app.route("/api/test-connection", methods=["POST"])
def api_test():
    try:
        get_client().test_connection()
        return jsonify({"ok": True, "message": "接続成功"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def api_download():
    if not start_job("download", job_download):
        return jsonify({"ok": False, "message": "別の処理が実行中です"}), 409
    return jsonify({"ok": True})


@app.route("/api/job")
def api_job():
    return jsonify(JOB)


def build_item_filter(args):
    """一覧・全選択で共通の絞り込み条件を組み立てる"""
    where, params = [], []
    q = args.get("q", "").strip()
    qfield = args.get("qfield", "all")
    if q:
        like = f"%{q}%"
        if qfield == "title":
            where.append("title LIKE ?"); params.append(like)
        elif qfield == "manage_number":
            where.append("manage_number LIKE ?"); params.append(like)
        elif qfield == "item_number":
            where.append("item_number LIKE ?"); params.append(like)
        elif qfield == "tagline":
            where.append("tagline LIKE ?"); params.append(like)
        elif qfield == "description":
            # 説明文はJSON本体を対象に検索
            where.append("COALESCE(edited, raw) LIKE ?"); params.append(like)
        else:
            where.append("(manage_number LIKE ? OR title LIKE ? OR tagline LIKE ? OR item_number LIKE ?)")
            params += [like] * 4
    only = args.get("only", "")  # edited / new / hidden / clean
    if only == "edited":
        where.append("(edited IS NOT NULL OR is_new=1)")
    elif only == "new":
        where.append("is_new=1")
    elif only == "hidden":
        where.append("hide_item=1")
    elif only == "clean":
        where.append("(edited IS NULL AND is_new=0)")
    elif only == "sale":
        where.append("sale_flag=1")
    # 列フィルター（Excel風）: <col>_vals = {"mode":"in"|"notin","vals":[...]} のJSON
    for col in ("manage_number", "title", "tagline", "item_number"):
        spec_raw = args.get(f"{col}_vals", "")
        if spec_raw:
            try:
                spec = json.loads(spec_raw)
            except ValueError:
                spec = None
            vals = (spec or {}).get("vals") or []
            if vals:
                vals = [str(v) for v in vals][:900]
                ph = ",".join("?" * len(vals))
                op = "NOT IN" if (spec or {}).get("mode") == "notin" else "IN"
                where.append(f"COALESCE({col},'') {op} ({ph})")
                params += vals
        text = args.get(f"{col}_text", "").strip()
        if text:
            where.append(f"{col} LIKE ?")
            params.append(f"%{text}%")
    hide = args.get("hide", "")  # 1=倉庫のみ / 0=表示のみ
    if hide in ("0", "1"):
        where.append("hide_item=?"); params.append(int(hide))
    pmin = args.get("price_min", "").strip()
    pmax = args.get("price_max", "").strip()
    if pmin:
        where.append("max_price >= ?"); params.append(float(pmin))
    if pmax:
        where.append("min_price <= ?"); params.append(float(pmax))
    genre = args.get("genre", "").strip()
    if genre:
        where.append("json_extract(COALESCE(edited, raw), '$.genreId') = ?")
        params.append(genre)
    return ("WHERE " + " AND ".join(where)) if where else "", params


@app.route("/api/items")
def api_items():
    page = max(int(request.args.get("page", 1)), 1)
    per = min(int(request.args.get("per", 50)), 200)
    wsql, params = build_item_filter(request.args)
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM items {wsql}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT manage_number,title,tagline,item_number,hide_item,min_price,max_price,"
            f"(edited IS NOT NULL) edited,is_new,sale_flag FROM items {wsql} ORDER BY manage_number LIMIT ? OFFSET ?",
            params + [per, (page - 1) * per]).fetchall()
    return jsonify({"total": total, "page": page, "per": per,
                    "items": [dict(r) for r in rows]})


FACET_COLUMNS = ("manage_number", "title", "tagline", "item_number")


@app.route("/api/facets")
def api_facets():
    """列フィルター用：指定列の値一覧と件数を返す（他の列の絞り込みを反映、自列の絞り込みは除外）"""
    col = request.args.get("column", "")
    if col not in FACET_COLUMNS:
        return jsonify({"message": "invalid column"}), 400
    args = {k: v for k, v in request.args.items()
            if k not in (f"{col}_vals", f"{col}_text", "column")}
    wsql, params = build_item_filter(args)
    with db() as conn:
        rows = conn.execute(
            f"SELECT COALESCE({col},'') v, COUNT(*) c FROM items {wsql} "
            f"GROUP BY COALESCE({col},'') ORDER BY v LIMIT 501", params).fetchall()
    truncated = len(rows) > 500
    return jsonify({"values": [{"v": r["v"], "c": r["c"]} for r in rows[:500]],
                    "truncated": truncated})


@app.route("/api/item-ids")
def api_item_ids():
    """絞り込み条件に一致する全商品の管理番号を返す（ページをまたぐ全選択用）"""
    wsql, params = build_item_filter(request.args)
    with db() as conn:
        rows = conn.execute(
            f"SELECT manage_number FROM items {wsql} ORDER BY manage_number LIMIT 20000",
            params).fetchall()
    return jsonify({"ids": [r["manage_number"] for r in rows]})


@app.route("/api/items/<mn>", methods=["GET", "PUT", "DELETE"])
def api_item(mn):
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE manage_number=?", (mn,)).fetchone()
    if request.method == "GET":
        if not row:
            return jsonify({"message": "not found"}), 404
        return jsonify({"item": get_effective(row), "is_new": row["is_new"],
                        "edited": bool(row["edited"])})
    if request.method == "DELETE":
        # ローカルの編集/新規を破棄（RMSからは削除しない）
        with db() as conn:
            if row and row["is_new"]:
                conn.execute("DELETE FROM items WHERE manage_number=?", (mn,))
            elif row:
                raw = json.loads(row["raw"])
                s = item_summary(raw)
                conn.execute(
                    """UPDATE items SET edited=NULL, title=?, tagline=?, item_number=?,
                       hide_item=?, min_price=?, max_price=? WHERE manage_number=?""",
                    (s["title"], s["tagline"], s["item_number"], s["hide_item"],
                     s["min_price"], s["max_price"], mn))
        return jsonify({"ok": True})
    # PUT: ローカル保存
    data = request.json or {}
    item = data.get("item")
    if not item:
        return jsonify({"message": "item がありません"}), 400
    item["manageNumber"] = mn
    save_local_edit(mn, item, is_new=(row["is_new"] if row else True) if data.get("isNew") is None else data["isNew"])
    return jsonify({"ok": True})


def _get_by_path(obj, path):
    cur = obj
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _set_by_path(obj, path, value):
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


@app.route("/api/bulk-edit", methods=["POST"])
def api_bulk_edit():
    data = request.json or {}
    targets = data.get("targets") or []
    op = data.get("op") or {}
    if not targets:
        return jsonify({"message": "対象商品が選択されていません"}), 400
    q = ",".join("?" * len(targets))
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM items WHERE manage_number IN ({q})", targets).fetchall()
    changed = 0
    for row in rows:
        item = get_effective(row)
        if not item:
            continue
        before = json.dumps(item, ensure_ascii=False)
        kind = op.get("kind")
        if kind == "replace":
            for field in op.get("fields") or []:
                val = _get_by_path(item, field)
                if isinstance(val, str) and op.get("search"):
                    _set_by_path(item, field, val.replace(op["search"], op.get("replace", "")))
        elif kind == "prepend_append":
            for field in op.get("fields") or []:
                val = _get_by_path(item, field) or ""
                if isinstance(val, str):
                    _set_by_path(item, field, op.get("prepend", "") + val + op.get("append", ""))
        elif kind == "price":
            mode = op.get("mode")
            value = float(op.get("value", 0))
            for v in (item.get("variants") or {}).values():
                if not isinstance(v, dict) or not isinstance(v.get("standardPrice"), (int, float)):
                    continue
                p = v["standardPrice"]
                if mode == "percent":
                    p = round(p * (1 + value / 100.0))
                elif mode == "delta":
                    p = p + value
                elif mode == "fixed":
                    p = value
                v["standardPrice"] = max(int(round(p)), 1)
        elif kind == "refprice":
            # 二重価格（表示価格）の一括設定/解除
            for v in (item.get("variants") or {}).values():
                if not isinstance(v, dict):
                    continue
                if op.get("clear"):
                    v.pop("referencePrice", None)
                    continue
                mode = op.get("mode", "fixed")
                value = float(op.get("value", 0))
                if mode == "percent":
                    base = v.get("standardPrice")
                    if not isinstance(base, (int, float)):
                        continue
                    val = base * (1 + value / 100.0)
                else:
                    val = value
                t = op.get("type", 1)
                rp = v.get("referencePrice") or {}
                rp["displayType"] = rp.get("displayType") or "REFERENCE_PRICE"
                rp["type"] = int(t) if str(t).lstrip("-").isdigit() else t
                rp["value"] = max(int(round(val)), 1)
                v["referencePrice"] = rp
        elif kind == "hide":
            item["hideItem"] = bool(op.get("value"))
        elif kind == "period":
            if op.get("clear"):
                item["purchasablePeriod"] = None
            else:
                period = {}
                if op.get("start"):
                    period["start"] = op["start"]
                if op.get("end"):
                    period["end"] = op["end"]
                item["purchasablePeriod"] = period or None
        else:
            return jsonify({"message": f"不明な操作: {kind}"}), 400
        if json.dumps(item, ensure_ascii=False) != before:
            save_local_edit(row["manage_number"], item, is_new=bool(row["is_new"]))
            changed += 1
    return jsonify({"ok": True, "changed": changed,
                    "message": f"{changed}件をローカル編集しました（[RMSに反映]で反映されます）"})


@app.route("/api/apply", methods=["POST"])
def api_apply():
    data = request.json or {}
    targets = data.get("targets") or []
    if not start_job("apply", job_apply, targets):
        return jsonify({"ok": False, "message": "別の処理が実行中です"}), 409
    return jsonify({"ok": True})


@app.route("/api/duplicate", methods=["POST"])
def api_duplicate():
    data = request.json or {}
    src, new = data.get("source"), (data.get("newManageNumber") or "").strip()
    if not re.match(r"^[a-z0-9\-_]{1,32}$", new or ""):
        return jsonify({"message": "新しい商品管理番号は半角英数小文字/ハイフン/アンダーバー32文字以内で指定してください"}), 400
    with db() as conn:
        row = conn.execute("SELECT * FROM items WHERE manage_number=?", (src,)).fetchone()
        dup = conn.execute("SELECT 1 FROM items WHERE manage_number=?", (new,)).fetchone()
    if not row:
        return jsonify({"message": "複製元が見つかりません"}), 404
    if dup:
        return jsonify({"message": f"商品管理番号 {new} は既に存在します"}), 400
    item = get_effective(row)
    item["manageNumber"] = new
    if data.get("newTitle"):
        item["title"] = data["newTitle"]
    save_local_edit(new, item, is_new=True)
    return jsonify({"ok": True, "message": f"{new} として複製しました（新規・未反映）"})


CSV_COLUMNS = [
    "manageNumber", "itemNumber", "title", "tagline",
    "productDescriptionPc", "productDescriptionSp", "salesDescription",
    "genreId", "taxIncluded", "hideItem",
    "imageLocation1", "imageLocation2", "imageLocation3",
    "variantId", "standardPrice", "referencePrice", "quantity",
]


@app.route("/api/csv-template")
def api_csv_template():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    w.writerow(["sample-001", "ITEM-0001", "サンプル商品名", "キャッチコピー",
                "<p>PC用商品説明文</p>", "スマホ用商品説明文", "<p>PC用販売説明文</p>",
                "100000", "1", "0", "/folder/image1.jpg", "", "",
                "sku-001", "1980", "2980", "10"])
    data = io.BytesIO(buf.getvalue().encode("cp932", errors="replace"))
    return send_file(data, mimetype="text/csv", as_attachment=True,
                     download_name="item_template.csv")


@app.route("/api/csv-import", methods=["POST"])
def api_csv_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"message": "ファイルがありません"}), 400
    raw = f.read()
    text = None
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return jsonify({"message": "文字コードを判定できません（UTF-8またはShift_JISで保存してください）"}), 400
    reader = csv.DictReader(io.StringIO(text))
    created, errors = 0, []
    for i, r in enumerate(reader, 2):
        try:
            mn = (r.get("manageNumber") or "").strip()
            if not mn:
                continue
            item = {
                "manageNumber": mn,
                "itemNumber": (r.get("itemNumber") or "").strip() or None,
                "title": (r.get("title") or "").strip(),
                "tagline": (r.get("tagline") or "").strip() or None,
                "productDescription": {
                    "pc": r.get("productDescriptionPc") or "",
                    "sp": r.get("productDescriptionSp") or "",
                },
                "salesDescription": r.get("salesDescription") or None,
                "itemType": "NORMAL",
                "genreId": (r.get("genreId") or "").strip(),
                "hideItem": (r.get("hideItem") or "0").strip() in ("1", "true", "TRUE"),
                "payment": {"taxIncluded": (r.get("taxIncluded") or "1").strip() in ("1", "true", "TRUE")},
            }
            images = []
            for k in ("imageLocation1", "imageLocation2", "imageLocation3"):
                loc = (r.get(k) or "").strip()
                if loc:
                    images.append({"type": "CABINET", "location": loc})
            if images:
                item["images"] = images
            vid = (r.get("variantId") or "sku-1").strip()
            variant = {}
            if (r.get("standardPrice") or "").strip():
                variant["standardPrice"] = int(float(r["standardPrice"]))
            if (r.get("referencePrice") or "").strip():
                variant["referencePrice"] = {"displayType": "REFERENCE_PRICE",
                                             "value": int(float(r["referencePrice"]))}
            item["variants"] = {vid: variant}
            if (r.get("quantity") or "").strip():
                item["_initialQuantity"] = int(float(r["quantity"]))
            item = {k: v for k, v in item.items() if v is not None}
            save_local_edit(mn, item, is_new=True)
            created += 1
        except Exception as e:
            errors.append(f"{i}行目: {e}")
    return jsonify({"ok": True, "created": created, "errors": errors,
                    "message": f"{created}件を取り込みました（新規・未反映）"
                               + (f" / エラー{len(errors)}件" if errors else "")})


@app.route("/api/csv-export")
def api_csv_export():
    with db() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY manage_number").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for row in rows:
        item = get_effective(row) or {}
        pd = item.get("productDescription") or {}
        images = item.get("images") or []
        locs = [img.get("location", "") for img in images[:3]] + ["", "", ""]
        variants = item.get("variants") or {}
        vid = next(iter(variants), "")
        v = variants.get(vid) or {}
        ref = v.get("referencePrice") or {}
        w.writerow([
            item.get("manageNumber", row["manage_number"]), item.get("itemNumber", ""),
            item.get("title", ""), item.get("tagline", ""),
            pd.get("pc", ""), pd.get("sp", ""), item.get("salesDescription", ""),
            item.get("genreId", ""),
            1 if (item.get("payment") or {}).get("taxIncluded") else 0,
            1 if item.get("hideItem") else 0,
            locs[0], locs[1], locs[2],
            vid, v.get("standardPrice", ""), ref.get("value", ""), "",
        ])
    data = io.BytesIO(buf.getvalue().encode("cp932", errors="replace"))
    return send_file(data, mimetype="text/csv", as_attachment=True,
                     download_name="items_export.csv")


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """配布用リセット：APIキー・商品データ・ログをすべて消去して初期状態に戻す"""
    global _mock
    try:
        if os.path.exists(CONFIG_PATH):
            os.remove(CONFIG_PATH)
    except OSError:
        save_config(dict(DEFAULT_CONFIG))
    with db() as conn:
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM logs")
    _mock = None
    return jsonify({"ok": True,
                    "message": "初期化しました。APIキー・商品データ・ログをすべて削除し、デモモードに戻りました。"})


@app.route("/api/logs")
def api_logs():
    with db() as conn:
        rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 300").fetchall()
    return jsonify({"logs": [dict(r) for r in rows]})


@app.route("/api/stats")
def api_stats():
    with db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) total,"
            " SUM(CASE WHEN edited IS NOT NULL OR is_new=1 THEN 1 ELSE 0 END) pending,"
            " SUM(is_new) new_items FROM items").fetchone()
    return jsonify({"total": r["total"], "pending": r["pending"] or 0, "new": r["new_items"] or 0})


def pick_free_port(start):
    """指定ポートが使用中（古いバージョンが起動中など）なら空いているポートを探す"""
    import socket
    for p in range(start, start + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
    return start


LOG_PATH = os.path.join(BASE_DIR, "起動ログ.txt")


def setup_windowed_io():
    """コンソール非表示のEXE（PyInstaller --noconsole）では sys.stdout/stderr が
    None になり、print() やFlaskのログ出力が例外を起こす。ログファイルへ振り向ける。
    通常のコンソール実行時は何もしない。"""
    if sys.stdout is not None and sys.stderr is not None:
        return False
    try:
        f = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except Exception:
        f = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr = f
    print(f"\n===== 起動 {time.strftime('%Y-%m-%d %H:%M:%S')} v{APP_VERSION} =====")
    return True


def show_error_dialog(message):
    """コンソールが無くてもユーザーにエラーを見せる。"""
    if sys.platform == "win32":
        try:
            import ctypes
            # 0x10 = MB_ICONERROR
            ctypes.windll.user32.MessageBoxW(None, message, "RMS商品一括編集ツール", 0x10)
            return
        except Exception:
            pass
    try:
        print(message)
    except Exception:
        pass


if __name__ == "__main__":
    windowed = setup_windowed_io()
    try:
        init_db()
        threading.Thread(target=scheduler_loop, daemon=True).start()
        port = pick_free_port(int(os.environ.get("PORT", 8930)))
        print(f"* RMS商品一括編集ツール v{APP_VERSION}: http://localhost:{port} をブラウザで開いてください")
        if port != int(os.environ.get("PORT", 8930)):
            print("* 注意: 標準ポートが使用中のため別ポートで起動しました。"
                  "古いバージョンのツールが起動したままの可能性があります。")
        try:
            threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()
        except Exception:
            pass
        app.run(host="127.0.0.1", port=port, debug=False)
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        try:
            print(tb)
        except Exception:
            pass
        last = tb.strip().splitlines()[-1] if tb.strip() else "不明なエラー"
        msg = ("ツールの起動に失敗しました。\n\n"
               f"{last}\n\n"
               "よくある原因:\n"
               "・書き込みできない場所（ZIP内・Program Files等）に置いている\n"
               "・ウイルス対策ソフトにブロックされている\n\n")
        if windowed:
            msg += f"詳細ログ:\n{LOG_PATH}"
        show_error_dialog(msg)
        raise SystemExit(1)

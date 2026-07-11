# -*- coding: utf-8 -*-
"""楽天RMS Item API 2.0 クライアント"""
import base64
import copy
import json
import threading
import time

import requests

API_BASE = "https://api.rms.rakuten.co.jp/es/2.0"


class RmsError(Exception):
    def __init__(self, status, message, body=None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class RmsClient:
    """RMS Item API 2.0 / Inventory API 2.0 クライアント（要 WEB APIサービス申請）"""

    def __init__(self, service_secret, license_key, wait_ms=700):
        self.service_secret = service_secret.strip()
        self.license_key = license_key.strip()
        self.wait_ms = wait_ms
        self._last_request = 0.0
        self._lock = threading.Lock()

    def _headers(self):
        raw = f"{self.service_secret}:{self.license_key}".encode("utf-8")
        return {
            "Authorization": "ESA " + base64.b64encode(raw).decode("ascii"),
            "Content-Type": "application/json; charset=utf-8",
        }

    def _throttle(self):
        with self._lock:
            wait = self.wait_ms / 1000.0 - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()

    def _request(self, method, path, params=None, body=None):
        self._throttle()
        url = API_BASE + path
        resp = requests.request(
            method, url, headers=self._headers(), params=params,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            timeout=60,
        )
        text = resp.text
        data = None
        if text:
            try:
                data = resp.json()
            except ValueError:
                data = {"rawText": text}
        if resp.status_code >= 400:
            msg = ""
            if isinstance(data, dict):
                errs = data.get("errors") or []
                if errs:
                    msg = "; ".join(
                        f"{e.get('code','')} {e.get('message','')}".strip() for e in errs if isinstance(e, dict)
                    )
                if not msg:
                    msg = data.get("message") or data.get("rawText") or text[:500]
            raise RmsError(resp.status_code, msg or "unknown error", data)
        return data

    # ---- Item API 2.0 ----
    def test_connection(self):
        self.search_items({"hits": 1})
        return True

    def search_items(self, params):
        return self._request("GET", "/items/search", params=params) or {}

    def iter_all_items(self, progress_cb=None, stop_flag=None):
        """全商品をページングで取得して yield する。cursorMark優先、失敗時offset。"""
        hits = 100
        cursor = "*"
        use_cursor = True
        offset = 0
        fetched = 0
        num_found = None
        while True:
            if stop_flag and stop_flag():
                return
            params = {"hits": hits}
            if use_cursor:
                params["cursorMark"] = cursor
            else:
                params["offset"] = offset
            try:
                data = self.search_items(params)
            except RmsError as e:
                if use_cursor and e.status == 400:
                    use_cursor = False
                    continue
                raise
            if num_found is None:
                num_found = data.get("numFound")
            results = data.get("results") or data.get("items") or []
            items = []
            for r in results:
                if isinstance(r, dict):
                    items.append(r.get("item", r))
            if not items:
                return
            for it in items:
                fetched += 1
                yield it
            if progress_cb:
                progress_cb(fetched, num_found)
            if use_cursor:
                next_cursor = data.get("nextCursorMark") or data.get("cursorMark")
                if not next_cursor or next_cursor == cursor:
                    # カーソルが進まない場合はoffsetに切替
                    if len(items) < hits:
                        return
                    use_cursor = False
                    offset = fetched
                else:
                    cursor = next_cursor
                    if num_found is not None and fetched >= num_found:
                        return
            else:
                if len(items) < hits:
                    return
                offset += hits
                if num_found is not None and offset >= num_found:
                    return

    def get_item(self, manage_number):
        return self._request("GET", f"/items/manage-numbers/{manage_number}")

    def upsert_item(self, manage_number, item):
        return self._request("PUT", f"/items/manage-numbers/{manage_number}", body=item)

    def patch_item(self, manage_number, partial):
        return self._request("PATCH", f"/items/manage-numbers/{manage_number}", body=partial)

    def delete_item(self, manage_number):
        return self._request("DELETE", f"/items/manage-numbers/{manage_number}")

    # ---- Inventory API 2.0 ----
    def bulk_upsert_inventory(self, inventories):
        """inventories: [{manageNumber, variantId, mode:'ABSOLUTE', quantity}] 最大400件"""
        return self._request("POST", "/inventories/bulk-upsert", body={"inventories": inventories})


class MockRmsClient:
    """デモモード用モック。APIキー不要でツールの動作確認ができる。"""

    def __init__(self, *args, **kwargs):
        self.wait_ms = 0
        self._store = {}
        for i in range(1, 31):
            mn = f"demo-{i:03d}"
            self._store[mn] = {
                "manageNumber": mn,
                "itemNumber": f"ITEM-{i:04d}",
                "title": f"【デモ】サンプル商品 その{i} 送料無料",
                "tagline": f"お買い得キャッチコピー {i}",
                "productDescription": {
                    "pc": f"<p>PC用商品説明文 サンプル{i}</p>",
                    "sp": f"スマホ用商品説明文 サンプル{i}",
                },
                "salesDescription": f"<p>PC用販売説明文 サンプル{i}</p>",
                "itemType": "NORMAL",
                "genreId": "100000",
                "hideItem": (i % 7 == 0),
                "payment": {"taxIncluded": True},
                "features": {"searchVisibility": "ALWAYS_VISIBLE"},
                "images": [{"type": "CABINET", "location": f"/demo/img{i}.jpg"}],
                "variants": {
                    f"sku-{i:03d}": {
                        "standardPrice": 1000 + i * 100,
                        "hidden": False,
                    }
                },
            }

    def test_connection(self):
        return True

    def iter_all_items(self, progress_cb=None, stop_flag=None):
        items = list(self._store.values())
        for n, it in enumerate(items, 1):
            time.sleep(0.03)
            yield copy.deepcopy(it)
            if progress_cb:
                progress_cb(n, len(items))

    def get_item(self, manage_number):
        if manage_number not in self._store:
            raise RmsError(404, "item not found (demo)")
        return copy.deepcopy(self._store[manage_number])

    def upsert_item(self, manage_number, item):
        item = copy.deepcopy(item)
        item["manageNumber"] = manage_number
        self._store[manage_number] = item
        return {}

    def patch_item(self, manage_number, partial):
        if manage_number not in self._store:
            raise RmsError(404, "item not found (demo)")
        self._store[manage_number].update(copy.deepcopy(partial))
        return {}

    def delete_item(self, manage_number):
        self._store.pop(manage_number, None)
        return {}

    def bulk_upsert_inventory(self, inventories):
        return {}

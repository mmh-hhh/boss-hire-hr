from __future__ import annotations


FAVORITE_LIST_URL = "https://www.zhipin.com/wapi/zprelation/bossTag/bossGetGeek"
FAVORITE_LIST_TAG = 4
FAVORITE_LIST_PAGE_SIZE = 10
FAVORITE_LIST_MAX_CANDIDATES = 400
FAVORITE_LIST_MAX_PAGES = FAVORITE_LIST_MAX_CANDIDATES // FAVORITE_LIST_PAGE_SIZE
FAVORITE_SYNC_RECEIPT_CONTRACT = "boss_favorite_sync_receipt"

SYNC_MODES = {"initialize", "incremental"}
SYNC_PURPOSES = {"publish", "favorite_delivery"}

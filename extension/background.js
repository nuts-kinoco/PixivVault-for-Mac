// PixivVault Extension Background Service Worker

const SERVER_URL = "http://127.0.0.1:25010";
const FETCH_TIMEOUT_MS = 8000;

// PixivVaultプロセスがフリーズ/デッドロックしている場合、応答が永久に返らずボタンが
// 「送信中...」のままハングし続けるのを防ぐため、タイムアウト付きfetchを使用する。
function fetchWithTimeout(url, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { ...options, signal: controller.signal }).finally(() => clearTimeout(timer));
}

// fetch自体が失敗した(=サーバーに接続すらできなかった)場合の分類。
// ブラウザのエラーメッセージ文言に依存せず、Errorの型で判定する。
function classifyFetchError(err) {
    return err && err.name === 'AbortError' ? 'timeout' : 'network';
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "sendDownloadRequest") {
        fetchWithTimeout(`${SERVER_URL}/download`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(request.payload)
        })
        .then(async (response) => {
            if (response.ok) {
                sendResponse({ success: true });
            } else {
                // サーバーには接続できたが、アプリ側がエラーを返した(=起動待ちではない)ケース。
                const text = await response.text();
                sendResponse({ success: false, error: text, kind: 'http' });
            }
        })
        .catch((err) => {
            sendResponse({ success: false, error: err.toString(), kind: classifyFetchError(err) });
        });
        return true; // 非同期で sendResponse を返すため true を返す
    } else if (request.action === "fetchStatus") {
        fetchWithTimeout(`${SERVER_URL}${request.path}`)
        .then(res => {
            if (!res.ok) {
                return res.text().then(text => { throw new Error(text || `HTTP ${res.status}`); });
            }
            return res.json();
        })
        .then(data => sendResponse({ success: true, data: data }))
        .catch(err => sendResponse({ success: false, error: err.toString(), kind: classifyFetchError(err) }));
        return true;
    } else if (request.action === "syncCookies") {
        syncCookiesToServer().then(res => sendResponse({ success: true, ...res })).catch(err => sendResponse({ success: false, error: err.toString() }));
        return true;
    }
});

// PixivのCookieを取得しローカルサーバーの /api/cookie/sync に送信する
async function syncCookiesToServer() {
    try {
        const cookies = await chrome.cookies.getAll({ domain: "pixiv.net" });
        if (!cookies || cookies.length === 0) {
            return { synced: false, reason: "No cookies found" };
        }
        const response = await fetchWithTimeout(`${SERVER_URL}/api/cookie/sync`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ cookies: cookies })
        }, 5000);
        if (response.ok) {
            return { synced: true };
        } else {
            return { synced: false, status: response.status };
        }
    } catch (e) {
        // アプリケーション（ローカルサーバー）が起動していない場合は静かに無視
        return { synced: false, error: e.toString() };
    }
}

// アラーム設定および起動時・インストール時にCookie自動同期を実行
chrome.runtime.onInstalled.addListener(() => {
    chrome.alarms.create("sync_cookies", { periodInMinutes: 30 });
    syncCookiesToServer();
});

chrome.runtime.onStartup.addListener(() => {
    syncCookiesToServer();
});

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "sync_cookies") {
        syncCookiesToServer();
    }
});


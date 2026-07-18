// PixivVault Web Extension Content Script

const SERVER_URL = "http://127.0.0.1:25010/download";
const FETCH_TIMEOUT_MS = 8000;

// background.js からの応答がない場合(サービスワーカー未応答等)の直接fetchフォールバックが
// 永久にハングしないよう、タイムアウト付きfetchを使用する。
function fetchWithTimeout(url, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(url, { ...options, signal: controller.signal }).finally(() => clearTimeout(timer));
}

// fetch自体が失敗した(=サーバーに接続すらできなかった)場合の分類。
// ブラウザのエラーメッセージ文言(「Failed to fetch」等)に依存せず、Errorの型で判定する。
function classifyFetchError(err) {
    return err && err.name === 'AbortError' ? 'timeout' : 'network';
}

async function fetchStatusFromServer(path) {
    if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
            const res = await chrome.runtime.sendMessage({ action: "fetchStatus", path: path });
            if (res && res.success) return res.data;
            return null;
        } catch (e) {}
    }
    try {
        const res = await fetchWithTimeout(`http://127.0.0.1:25010${path}`);
        if (!res.ok) return null;
        return await res.json();
    } catch (e) {
        return null;
    }
}

async function requestToServer(payload) {
    if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        try {
            const res = await chrome.runtime.sendMessage({ action: "sendDownloadRequest", payload: payload });
            if (res && res.success) {
                return { ok: true };
            } else if (res && !res.success) {
                return { ok: false, error: res.error, kind: res.kind };
            }
        } catch (e) {
            console.warn("Service Worker send failed, falling back to direct fetch:", e);
        }
    }
    try {
        const response = await fetchWithTimeout(SERVER_URL, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(payload)
        });
        return {
            ok: response.ok,
            error: response.ok ? null : await response.text(),
            kind: response.ok ? null : 'http'
        };
    } catch (err) {
        return { ok: false, error: err.toString(), kind: classifyFetchError(err) };
    }
}

async function sendDownloadRequest(payload, buttonElement, isRetry = false, retryCount = 0) {
    if (!isRetry) {
        buttonElement.innerText = "送信中...";
        buttonElement.disabled = true;
    }

    try {
        const result = await requestToServer(payload);

        if (result.ok) {
            buttonElement.innerText = "✓ 送信完了";
            buttonElement.classList.add("pv-success");
        } else {
            // 'network'/'timeout' はサーバーに接続すらできなかったケース(アプリ未起動の可能性)。
            // 自動起動・再送信フローに乗せる。'http' はアプリが実際にエラーを返したケースなので
            // 起動待ちとして扱わず、そのまま失敗表示する。
            if (result.kind === 'network' || result.kind === 'timeout') {
                throw new Error(result.error);
            }
            buttonElement.innerText = "❌ 失敗";
            buttonElement.classList.add("pv-error");
            console.error("PixivVault Server Error:", result.error);
        }
    } catch (err) {
        if (!isRetry && retryCount === 0) {
            console.warn("PixivVault Fetch Error. Attempting to start the app...", err);
            buttonElement.innerText = "アプリ起動中...";
            buttonElement.classList.remove("pv-error");
            
            window.location.href = "pixivvault://start";
            
            setTimeout(() => {
                buttonElement.innerText = "再送信中...";
                sendDownloadRequest(payload, buttonElement, true, 1);
            }, 3500);
            return;
        } else if (retryCount > 0 && retryCount < 7) {
            buttonElement.innerText = `起動待機中(${retryCount}/7)...`;
            setTimeout(() => {
                sendDownloadRequest(payload, buttonElement, true, retryCount + 1);
            }, 2500);
            return;
        } else {
            buttonElement.innerText = "❌ 接続エラー";
            buttonElement.classList.add("pv-error");
            console.error("PixivVault Fetch Retry Error:", err);
            alert("PixivVaultサーバーに接続できませんでした。\nアプリが起動していることと、ポート(25010)で通信できる状態であるかご確認ください。");
        }
    }

    setTimeout(() => {
        buttonElement.innerText = (payload.type === 'user') ? "差分DL" : "📥 PixivVaultに保存";
        buttonElement.disabled = false;
        buttonElement.classList.remove("pv-success", "pv-error");
    }, 3000);
}

function createVaultButton(text, onClick) {
    const btn = document.createElement("button");
    btn.className = "pixiv-vault-btn";
    btn.innerText = text;
    btn.onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        onClick(btn);
    };
    return btn;
}

// ==========================================
// 1. 作品単体ページ (/artworks/ID)
// ==========================================
function injectArtworkButton() {
    const match = window.location.pathname.match(/^\/artworks\/(\d+)/);
    if (!match) return;
    
    const workId = match[1];
    
    // PixivのUIはSPAで動的に変わるため、すでにボタンがあればスキップ
    if (document.getElementById(`pv-artwork-${workId}`)) return;

    // 「いいね」「ブックマーク」などのアクションバーを探す (Pixivのクラスや構造は変わりやすいので複数候補)
    const actionBars = document.querySelectorAll('section figure, aside section');
    if (actionBars.length === 0) return;
    
    // とりあえず見つけやすい場所に注入する（タイトル横やブックマークボタン付近）
    const targetParent = document.querySelector('main section > div > div > h1')?.parentElement?.parentElement || document.body;
    
    const btn = createVaultButton("📥 PixivVaultに保存", (btnEl) => {
        sendDownloadRequest({ type: "work", work_id: workId, is_novel: false }, btnEl);
    });
    btn.id = `pv-artwork-${workId}`;
    btn.classList.add("pv-floating-btn"); // 画面右下に固定配置するフォールバック
    
    document.body.appendChild(btn);

    fetchStatusFromServer(`/api/work/${workId}`).then(data => {
        if (data && data.downloaded) {
            btn.innerText = "✅ 保存済";
            btn.classList.add("pv-success");
        }
    });
}

// ==========================================
// 2. 小説単体ページ (/novel/show.php?id=ID)
// ==========================================
function injectNovelButton() {
    const params = new URLSearchParams(window.location.search);
    const novelId = params.get('id');
    if (!novelId || !window.location.pathname.includes('/novel/show.php')) return;

    if (document.getElementById(`pv-novel-${novelId}`)) return;

    const btn = createVaultButton("📥 PixivVaultに保存", (btnEl) => {
        sendDownloadRequest({ type: "work", work_id: novelId, is_novel: true }, btnEl);
    });
    btn.id = `pv-novel-${novelId}`;
    btn.classList.add("pv-floating-btn");
    
    document.body.appendChild(btn);

    fetchStatusFromServer(`/api/work/${novelId}`).then(data => {
        if (data && data.downloaded) {
            btn.innerText = "✅ 保存済";
            btn.classList.add("pv-success");
        }
    });
}

// ==========================================
// 3. フォロー中一覧ページ等のユーザーカード
// ==========================================
function injectUserButtons() {
    const userLinks = document.querySelectorAll('a[href^="/users/"]');
    
    // 現在のページで処理済みのuserIdを記録
    const processedUsers = new Set();
    
    userLinks.forEach(link => {
        const href = link.getAttribute('href');
        const match = href.match(/^\/users\/(\d+)(\/|$)/);
        if (!match) return;
        
        const userId = match[1];
        
        // 1ユーザーにつき1回だけ処理 (最初に見つかるリンク＝アイコン画像を想定)
        if (processedUsers.has(userId)) return;
        processedUsers.add(userId);
        
        const btnId = `pv-user-${userId}`;
        if (document.getElementById(btnId)) return;
        
        const btn = createVaultButton("差分DL", (btnEl) => {
            sendDownloadRequest({ type: "user", user_id: userId, new_only: true }, btnEl);
        });
        btn.id = btnId;
        btn.classList.add("pv-small-btn");
        // アイコンの左に配置するためのスタイル調整
        btn.style.marginRight = "16px";
        btn.style.marginLeft = "0";
        btn.style.flexShrink = "0";
        
        const parent = link.parentElement;
        if (parent) {
            const statusBox = document.createElement("span");
            statusBox.className = "pv-status-box";
            statusBox.style.width = "65px";
            statusBox.style.minWidth = "65px";
            statusBox.style.flexShrink = "0";
            statusBox.style.textAlign = "right";
            statusBox.style.whiteSpace = "nowrap";
            statusBox.style.marginRight = "8px";
            statusBox.style.fontSize = "12px";
            statusBox.style.color = "#2ecc71";
            statusBox.innerText = "";
            
            parent.insertBefore(btn, link);
            parent.insertBefore(statusBox, btn);
            parent.style.display = 'flex';
            parent.style.alignItems = 'center';
            
            fetchStatusFromServer(`/api/user/${userId}/status`).then(data => {
                if (data && data.downloaded && data.downloaded > 0) {
                    statusBox.innerText = `☑ ${data.downloaded}件`;
                }
            });
        }
    });
}

// ==========================================
// SPA遷移対応 (MutationObserver)
// ==========================================
const observer = new MutationObserver((mutations) => {
    injectArtworkButton();
    injectNovelButton();
    
    // 特定のページのみユーザーボタンを注入
    if (window.location.pathname.includes('/following') || window.location.pathname.includes('/users/')) {
        injectUserButtons();
    }
});

observer.observe(document.body, { childList: true, subtree: true });

// 初回実行
injectArtworkButton();
injectNovelButton();
if (window.location.pathname.includes('/following') || window.location.pathname.includes('/users/')) {
    injectUserButtons();
}

// ページロード時およびフォーカス時に自動Cookie同期をリクエスト (最短3分間隔で制限)
let lastCookieSyncRequestTime = 0;
function requestCookieSync() {
    const now = Date.now();
    if (now - lastCookieSyncRequestTime < 180 * 1000) {
        return;
    }
    lastCookieSyncRequestTime = now;
    if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
        chrome.runtime.sendMessage({ action: "syncCookies" }).catch(() => {});
    }
}
requestCookieSync();
window.addEventListener('focus', requestCookieSync);


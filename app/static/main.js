const ADMIN_TOKEN_STORAGE_KEY = "hls_admin_token";
const ADMIN_USER_STORAGE_KEY = "hls_admin_user";
let videosHydratedFromApi = false;

function getAdminToken() {
    return localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY) || "";
}

function clearAdminSession() {
    localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
    localStorage.removeItem(ADMIN_USER_STORAGE_KEY);
}

function getAuthHeaders() {
    const token = getAdminToken();
    if (!token) {
        return {};
    }
    return { Authorization: `Bearer ${token}` };
}

function updateAdminStatus() {
    const status = document.getElementById("adminStatus");
    const username = localStorage.getItem(ADMIN_USER_STORAGE_KEY) || "";
    const token = getAdminToken();
    status.innerText = token
        ? `Режим администратора включен (${username || "admin"})`
        : "Режим администратора выключен";
}

document.getElementById("adminLoginBtn").addEventListener("click", async () => {
    const username = document.getElementById("adminUsername").value.trim();
    const passwordInput = document.getElementById("adminPassword");
    const password = passwordInput.value.trim();
    if (!username || !password) {
        alert("Введите логин и пароль");
        return;
    }
    try {
        const response = await fetch("/admin/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password })
        });
        const data = await response.json();
        if (!response.ok) {
            alert("Ошибка входа: " + (data.detail || "неизвестная ошибка"));
            return;
        }
        localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, data.access_token);
        localStorage.setItem(ADMIN_USER_STORAGE_KEY, data.username);
        passwordInput.value = "";
        updateAdminStatus();
        alert("Вход выполнен");
        await loadLogs();
    } catch (error) {
        alert("Сетевая ошибка при входе");
    }
});

document.getElementById("adminLogoutBtn").addEventListener("click", async () => {
    try {
        await fetch("/admin/logout", { method: "POST", headers: getAuthHeaders() });
    } catch (e) {
        console.warn("Не удалось завершить серверную сессию", e);
    }
    clearAdminSession();
    updateAdminStatus();
    await loadVideos();
    await loadLogs();
});

document.getElementById('uploadForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fileInput = document.getElementById('videoFile');
    const statusMsg = document.getElementById('statusMsg');
    if (fileInput.files.length === 0) return;

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);

    statusMsg.innerText = "Загрузка...";

    try {
        const response = await fetch('/upload/', {
            method: 'POST',
            body: formData
        });

        if (response.ok) {
            statusMsg.innerText = "Файл загружен и отправлен в обработку.";
            await loadVideos();
            await loadLogs();
        } else {
            const err = await response.json();
            statusMsg.innerText = "Ошибка: " + err.detail;
        }
    } catch (error) {
        statusMsg.innerText = "Сетевая ошибка!";
    }
});
async function deleteVideo(videoId) {
    const adminToken = getAdminToken();
    if (!adminToken) {
        alert("Удаление доступно только администратору");
        return;
    }
    if (!confirm("Вы уверены, что хотите удалить это видео?")) return;

    try {
        const response = await fetch(`/delete/${videoId}`, {
            method: 'POST',
            headers: getAuthHeaders()
        });

        if (response.ok) {
            alert("Видео удалено");
            await loadVideos();
            await loadLogs();
        } else {
            const err = await response.json();
            alert("Ошибка при удалении: " + (err.detail || "неизвестная ошибка"));
        }
    } catch (error) {
        console.error("Сетевая ошибка:", error);
    }
};

function createVideoCard(video) {
    const container = document.createElement("div");
    container.className = "video-container";
    container.dataset.videoId = String(video.id);

    const title = document.createElement("p");
    title.className = "video-title";
    title.innerHTML = `<strong>${video.filename}</strong> - Статус: ${video.status}`;
    container.appendChild(title);

    if (getAdminToken()) {
        const delBtn = document.createElement("button");
        delBtn.innerText = "Удалить";
        delBtn.style.color = "red";
        delBtn.style.cursor = "pointer";
        delBtn.onclick = () => deleteVideo(video.id);
        container.appendChild(delBtn);
    }

    if (video.status === "ready") {
        const videoEl = document.createElement("video");
        videoEl.controls = true;
        container.appendChild(videoEl);
        if (video.stream_url && window.Hls && window.Hls.isSupported()) {
            const hls = new window.Hls();
            hls.loadSource(video.stream_url);
            hls.attachMedia(videoEl);
        } else if (video.stream_url && videoEl.canPlayType("application/vnd.apple.mpegurl")) {
            videoEl.src = video.stream_url;
        } else if (video.source_url) {
            // Fallback: прямое воспроизведение исходного файла (например, MP4)
            videoEl.src = video.source_url;
            videoEl.type = "video/mp4";
        } else {
            const note = document.createElement("p");
            note.innerText = "Плеер недоступен: поток еще не готов или не поддерживается браузером.";
            container.appendChild(note);
        }
    }
    return container;
}

function updateVideoCard(card, video) {
    const title = card.querySelector(".video-title");
    if (title) {
        title.innerHTML = `<strong>${video.filename}</strong> - Статус: ${video.status}`;
    }

    const hasPlayer = Boolean(card.querySelector("video"));
    if (!hasPlayer && video.status === "ready") {
        const newCard = createVideoCard(video);
        const oldVideo = card.querySelector("video");
        if (!oldVideo) {
            const oldDeleteBtn = card.querySelector("button");
            if (oldDeleteBtn) oldDeleteBtn.remove();
            while (card.firstChild) card.removeChild(card.firstChild);
            Array.from(newCard.childNodes).forEach((node) => card.appendChild(node));
        }
    }
}

async function loadVideos() {
    const videosContainer = document.getElementById("videosContainer");
    try {
        const response = await fetch("/videos");
        if (!response.ok) {
            return;
        }
        const videos = await response.json();
        if (!videosHydratedFromApi) {
            videosContainer.innerHTML = "";
            videosHydratedFromApi = true;
        }
        const existingCards = new Map();
        videosContainer.querySelectorAll(".video-container[data-video-id]").forEach((card) => {
            existingCards.set(card.dataset.videoId, card);
        });

        const seen = new Set();
        if (!videos.length) {
            videosContainer.innerHTML = "<p>Нет загруженных видео.</p>";
            return;
        }

        // Удаляем заглушку "Нет загруженных видео", если она была.
        if (videosContainer.children.length === 1 && videosContainer.textContent.includes("Нет загруженных видео")) {
            videosContainer.innerHTML = "";
        }

        videos.forEach((video) => {
            const key = String(video.id);
            seen.add(key);
            if (existingCards.has(key)) {
                updateVideoCard(existingCards.get(key), video);
            } else {
                videosContainer.appendChild(createVideoCard(video));
            }
        });

        existingCards.forEach((card, key) => {
            if (!seen.has(key)) {
                card.remove();
            }
        });
    } catch (e) {
        console.error("Не удалось обновить список видео", e);
    }
}

async function loadLogs() {
    const logsContainer = document.getElementById("logsContainer");
    if (!getAdminToken()) {
        logsContainer.innerHTML = "<li>Войдите как администратор, чтобы видеть журнал.</li>";
        return;
    }
    try {
        const response = await fetch("/admin/logs", { headers: getAuthHeaders() });
        if (!response.ok) {
            logsContainer.innerHTML = "<li>Нет доступа к журналу.</li>";
            return;
        }
        const logs = await response.json();
        logsContainer.innerHTML = "";
        if (!logs.length) {
            logsContainer.innerHTML = "<li>Журнал пуст.</li>";
            return;
        }
        logs.forEach((log) => {
            const li = document.createElement("li");
            li.innerText = `[${new Date(log.created_at).toLocaleString()}] ${log.action}: ${log.details || "-"}`;
            logsContainer.appendChild(li);
        });
    } catch (e) {
        logsContainer.innerHTML = "<li>Ошибка загрузки журнала.</li>";
    }
}

updateAdminStatus();
loadVideos();
loadLogs();
setInterval(loadVideos, 5000);
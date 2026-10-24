"use strict";

document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

const telegramPage = document.getElementById("telegram-login");
if (telegramPage) {
  (async () => {
    const message = document.getElementById("telegram-status");
    const token = new URLSearchParams(window.location.hash.slice(1)).get(
      "token",
    );
    history.replaceState(null, "", window.location.pathname);
    if (!token) {
      message.textContent =
        "Пайвандро аз бот кушоед / Откройте ссылку из бота.";
      return;
    }
    try {
      async function exchange(confirm = false) {
        const response = await fetch("/api/auth/telegram", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token, confirm }),
          credentials: "same-origin",
        });
        const result = await response.json();
        return { response, result };
      }
      let { response, result } = await exchange();
      if (result.needs_confirmation) {
        const accepted = window.confirm(
          "Привязать Telegram «" +
            result.name +
            "» (ID " +
            result.telegram_id +
            ") к вашему аккаунту? Подтверждайте только свой Telegram. Если ссылку прислал другой человек, нажмите Отмена.",
        );
        if (!accepted) {
          message.textContent = "Привязка отменена. Аккаунт не изменён.";
          return;
        }
        ({ response, result } = await exchange(true));
      }
      if (!response.ok) throw new Error(result.detail || "Вход не выполнен");
      window.location.replace(result.redirect);
    } catch (error) {
      message.textContent = error.message;
    }
  })();
}

const scanner = document.getElementById("scanner");
if (scanner) {
  (async () => {
    const field = document.getElementById("track-input");
    const form = document.getElementById("scan-form");
    const alert = document.getElementById("scan-alert");
    const log = document.getElementById("scan-log");
    const scope =
      document.body.dataset.accountId + ":" + scanner.dataset.warehouse;
    const csrf = document.body.dataset.csrf;
    let database,
      running = false,
      stopped = false;
    function transaction(mode, action) {
      return new Promise((resolve, reject) => {
        const tx = database.transaction("scans", mode);
        let result;
        const request = action(tx.objectStore("scans"));
        if (request)
          request.onsuccess = () => {
            result = request.result;
          };
        tx.oncomplete = () => resolve(result);
        tx.onerror = () =>
          reject(tx.error || new Error("Ошибка сохранения очереди"));
        tx.onabort = () =>
          reject(tx.error || new Error("Очередь не сохранена"));
      });
    }
    const put = (row) => transaction("readwrite", (store) => store.put(row));
    const remove = (id) =>
      transaction("readwrite", (store) => store.delete(id));
    async function rows() {
      const all = await transaction("readonly", (store) =>
        store.index("scope").getAll(scope),
      );
      return all.sort((a, b) => a.created - b.created);
    }
    function connection(message, ok = true) {
      const element = document.getElementById("connection-state");
      element.textContent = (ok ? "● " : "○ ") + message;
      element.className = ok ? "text-success" : "muted";
    }
    function statusText(row) {
      if (row.state === "pending") return "Дар навбат / Ожидает сохранения";
      if (row.state === "failed") return row.error || "Санҷиш лозим";
      const result = row.result;
      if (result.outcome === "duplicate")
        return "Аллакай дар ҳамин анбор / Повторный скан";
      if (result.outcome === "ahead")
        return "Ҳолати навтар нигоҳ дошта шуд / Посылка уже дальше по маршруту";
      return (
        "✓ Дар база сабт шуд / Сохранено" +
        (result.notification === "no_telegram_owner"
          ? " · Нет привязанного Telegram"
          : " · Уведомление в очереди")
      );
    }
    async function render() {
      const all = await rows();
      document.getElementById("scan-ok").textContent = all.filter(
        (x) => x.state === "done",
      ).length;
      document.getElementById("scan-pending").textContent = all.filter(
        (x) => x.state === "pending",
      ).length;
      document.getElementById("scan-failed").textContent = all.filter(
        (x) => x.state === "failed",
      ).length;
      log.replaceChildren();
      for (const row of all.slice(-40).reverse()) {
        const item = document.createElement("div");
        item.className = "scan-log-row";
        const details = document.createElement("div");
        const code = document.createElement("strong");
        code.textContent = row.track;
        const text = document.createElement("small");
        text.textContent = statusText(row);
        if (row.state === "failed") text.className = "error-text";
        details.append(code, text);
        item.append(details);
        if (row.state === "failed") {
          const discard = document.createElement("button");
          discard.className = "danger-outline";
          discard.type = "button";
          discard.textContent = "Хориҷ кардан";
          discard.addEventListener("click", async () => {
            if (
              confirm(
                "Удалить неподтверждённый скан из очереди? Убедитесь, что код записан отдельно.",
              )
            ) {
              await remove(row.id);
              await render();
            }
          });
          item.append(discard);
        } else {
          const time = document.createElement("small");
          time.textContent = new Date(row.created).toLocaleTimeString("ru-RU");
          item.append(time);
        }
        log.append(item);
      }
      // Keep the latest 500 acknowledged scans. Unacknowledged scans are never pruned.
      const done = all.filter((x) => x.state === "done");
      for (const row of done.slice(0, Math.max(0, done.length - 500)))
        await remove(row.id);
    }
    async function processQueue() {
      if (running || stopped || !database) return;
      running = true;
      try {
        while (navigator.onLine && !stopped) {
          const row = (await rows()).find((x) => x.state === "pending");
          if (!row) {
            connection("Омода ба скан / Готово");
            break;
          }
          const controller = new AbortController();
          const timeout = setTimeout(() => controller.abort(), 15000);
          try {
            const response = await fetch("/api/scans", {
              method: "POST",
              credentials: "same-origin",
              signal: controller.signal,
              headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
              },
              body: JSON.stringify({
                warehouse: scanner.dataset.warehouse,
                track: row.track,
                request_id: row.id,
              }),
            });
            const result = await response.json();
            if (response.status === 401 || response.status === 403) {
              stopped = true;
              alert.textContent =
                "Сеанс завершён или права изменены. Войдите заново. Очередь сохранена в браузере.";
              break;
            }
            if (response.status === 429 || response.status >= 500)
              throw new Error("Сервер временно недоступен");
            if (!response.ok) {
              row.state = "failed";
              row.error = result.detail || "Некорректный код";
            } else {
              row.state = "done";
              row.result = result;
            }
            await put(row);
            await render();
            connection("Омода ба скан / Готово");
          } catch (error) {
            connection(
              "Нет подтверждения сервера. Очередь сохранена; повторим отправку.",
              false,
            );
            break;
          } finally {
            clearTimeout(timeout);
          }
        }
        if (!navigator.onLine)
          connection("Интернет нест. Коды остаются в очереди браузера.", false);
      } catch (error) {
        alert.textContent = "Ошибка очереди: " + error.message;
      } finally {
        running = false;
      }
    }
    try {
      database = await new Promise((resolve, reject) => {
        const request = indexedDB.open("tuman-scans-v1", 1);
        request.onupgradeneeded = () => {
          request.result
            .createObjectStore("scans", { keyPath: "id" })
            .createIndex("scope", "scope");
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
        request.onblocked = () =>
          reject(new Error("Закройте старую вкладку сканирования"));
      });
      if (!crypto.randomUUID)
        throw new Error("Для сканирования требуется HTTPS или localhost");
      await render();
      await processQueue();
    } catch (error) {
      alert.textContent =
        "Сканирование заблокировано: нельзя безопасно сохранить очередь. " +
        error.message;
      form.querySelector("button").disabled = true;
      field.disabled = true;
      return;
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const track = field.value.replace(/\s/g, "").toUpperCase();
      if (!track) return;
      field.value = "";
      field.focus();
      const row = {
        id: crypto.randomUUID(),
        scope,
        track,
        state: "pending",
        created: Date.now(),
      };
      try {
        await put(row);
        await render();
        await processQueue();
      } catch (error) {
        alert.textContent =
          "НЕ СОХРАНЕНО: " +
          track +
          ". Запишите код и повторите скан. " +
          error.message;
      }
    });
    document
      .getElementById("retry-scans")
      .addEventListener("click", async () => {
        for (const row of await rows())
          if (row.state === "failed") {
            row.state = "pending";
            await put(row);
          }
        await render();
        await processQueue();
      });
    window.addEventListener("online", processQueue);
    window.addEventListener("offline", () =>
      connection("Интернет нест. Очередь в браузере.", false),
    );
    setInterval(processQueue, 2500);
    document.addEventListener("click", (event) => {
      if (!event.target.closest("input,textarea,select,button,a"))
        field.focus();
    });
  })();
}

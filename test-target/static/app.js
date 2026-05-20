(function () {
  const form = document.getElementById("test-form");
  const promptEl = document.getElementById("test-prompt");
  const submitBtn = document.getElementById("test-submit");
  const chatLog = document.getElementById("test-chat-log");
  const statusEl = document.getElementById("test-status");

  if (!form || !promptEl || !submitBtn || !chatLog) {
    return;
  }

  function setStatus(text) {
    if (!statusEl) return;
    if (!text) {
      statusEl.hidden = true;
      statusEl.textContent = "";
      statusEl.classList.remove("is-active");
      return;
    }
    statusEl.hidden = false;
    statusEl.textContent = text;
    statusEl.classList.add("is-active");
  }

  function appendAssistantMessage(text) {
    const message = document.createElement("div");
    message.className = "chat-message is-assistant";
    message.dataset.testid = "assistant-message";

    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";

    const paragraph = document.createElement("p");
    paragraph.textContent = text;

    bubble.appendChild(paragraph);
    message.appendChild(bubble);
    chatLog.appendChild(message);
    message.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function syncSubmitState() {
    submitBtn.disabled = promptEl.value.trim().length === 0;
  }

  promptEl.addEventListener("input", syncSubmitState);

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    const text = promptEl.value.trim();
    if (!text) return;

    submitBtn.disabled = true;
    setStatus("Generating response…");

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text }),
    })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, status: response.status, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          const detail = result.body && result.body.detail;
          const message =
            typeof detail === "string"
              ? detail
              : "Request failed (" + result.status + ")";
          throw new Error(message);
        }
        appendAssistantMessage(result.body.response);
        promptEl.value = "";
      })
      .catch(function (err) {
        appendAssistantMessage("Error: " + (err && err.message ? err.message : "request failed"));
      })
      .finally(function () {
        setStatus("");
        syncSubmitState();
      });
  });
})();

const messagesElement = document.querySelector("#messages");
const conversationList = document.querySelector("#conversation-list");
const modelSelect = document.querySelector("#model-select");
const input = document.querySelector("#message-input");
const statusElement = document.querySelector("#status");
const sendButton = document.querySelector("#send-button");
const stopButton = document.querySelector("#stop-button");

let conversationId = null;
let activeController = null;

function addMessage(message) {
  const welcome = messagesElement.querySelector(".welcome-card");
  if (welcome) welcome.remove();

  const wrapper = document.createElement("article");
  wrapper.className = `message ${message.role}`;
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  if (message.role === "note") {
    const label = document.createElement("span");
    label.className = "note-label";
    label.textContent = "Note privée · non envoyée au modèle";
    bubble.append(label);
  }
  const text = document.createElement("span");
  text.className = "message-text";
  text.textContent = message.content;
  bubble.append(text);
  wrapper.append(bubble);

  if (message.role !== "system-notification" && message.role !== "note") {
    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = message.role === "user" ? "Toi" : "Study Buddy";
    wrapper.append(meta);
  }
  messagesElement.append(wrapper);
  messagesElement.scrollTop = messagesElement.scrollHeight;
  return text;
}

function showWelcome() {
  messagesElement.replaceChildren();
  const card = document.createElement("div");
  card.className = "welcome-card";
  const icon = document.createElement("span");
  icon.className = "welcome-icon";
  icon.textContent = "✦";
  const title = document.createElement("h3");
  title.textContent = "On apprend ensemble.";
  const detail = document.createElement("p");
  detail.textContent = "Pose une question sur l'IA, demande un indice ou ajoute une note personnelle.";
  card.append(icon, title, detail);
  messagesElement.append(card);
}

function setStatus(message, retryText) {
  statusElement.replaceChildren();
  statusElement.append(document.createTextNode(message));
  if (retryText) {
    const retry = document.createElement("button");
    retry.className = "retry-button";
    retry.type = "button";
    retry.textContent = "Réessayer";
    retry.addEventListener("click", () => sendMessage(retryText));
    statusElement.append(retry);
  }
}

async function loadModels() {
  const response = await fetch("/models");
  if (!response.ok) throw new Error("Impossible de charger la liste des modèles.");
  const result = await response.json();
  modelSelect.replaceChildren();
  for (const model of result.models) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    modelSelect.append(option);
  }
  modelSelect.value = result.default;
}

async function refreshConversations() {
  const response = await fetch("/conversations");
  if (!response.ok) throw new Error("Impossible de charger les conversations.");
  const conversations = await response.json();
  conversationList.replaceChildren();
  for (const conversation of conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `conversation-item${conversation.id === conversationId ? " active" : ""}`;
    button.textContent = conversation.preview || "Nouvelle conversation";
    button.title = button.textContent;
    button.addEventListener("click", () => selectConversation(conversation.id));
    conversationList.append(button);
  }
  return conversations;
}

async function createConversation() {
  const response = await fetch("/conversations", { method: "POST" });
  if (!response.ok) throw new Error("Impossible de créer une conversation.");
  const result = await response.json();
  conversationId = result.conversation_id;
  showWelcome();
  await refreshConversations();
  input.focus();
}

async function selectConversation(id) {
  conversationId = id;
  await loadMessages();
  await refreshConversations();
  setStatus("");
}

async function loadMessages() {
  if (!conversationId) {
    showWelcome();
    return;
  }
  const response = await fetch(`/conversations/${conversationId}/messages`);
  if (!response.ok) throw new Error("Impossible de charger les messages.");
  const messages = await response.json();
  showWelcome();
  if (messages.length) messagesElement.replaceChildren();
  for (const message of messages) addMessage(message);
  return messages;
}

function parseEvent(frame) {
  let eventName = "message";
  const dataLines = [];
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  return { eventName, data: dataLines.join("\n") };
}

async function sendMessage(text) {
  if (!text || activeController) return;
  if (!conversationId) await createConversation();
  statusElement.replaceChildren();
  input.value = "";
  addMessage({ role: "user", content: text });
  const assistantText = addMessage({ role: "assistant", content: "" });
  const assistantBubble = assistantText.closest(".message").querySelector(".message-bubble");
  const controller = new AbortController();
  activeController = controller;
  sendButton.disabled = true;
  stopButton.hidden = false;
  let complete = false;
  let reader;

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation_id: conversationId,
        message: text,
        model: modelSelect.value,
      }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "Le serveur n'a pas pu démarrer la réponse.");
    }

    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let separator = buffer.indexOf("\n\n");
      while (separator !== -1) {
        const frame = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        const { eventName, data } = parseEvent(frame);
        if (eventName === "error") {
          throw new Error(JSON.parse(data).detail || "La génération a échoué.");
        }
        if (eventName === "usage") {
          const usage = JSON.parse(data);
          if (usage.total_tokens !== undefined) {
            const meta = assistantText.closest(".message").querySelector(".message-meta");
            if (meta) meta.textContent = `Study Buddy · ${usage.total_tokens} tokens`;
          }
        } else if (data === "[DONE]") {
          complete = true;
        } else if (data) {
          const event = JSON.parse(data);
          if (typeof event.content === "string") assistantText.textContent += event.content;
          if (event.role === "system-notification") addMessage(event);
        }
        separator = buffer.indexOf("\n\n");
      }
      if (done) break;
    }
    if (!complete) throw new Error("Le flux s'est terminé sans confirmation de sauvegarde.");
    await loadMessages();
    await refreshConversations();
  } catch (error) {
    if (error.name === "AbortError") {
      const savedMessages = await loadMessages();
      const turnWasSaved = savedMessages.some(
        (message, index) =>
          message.role === "user" &&
          message.content === text &&
          savedMessages[index + 1]?.role === "assistant",
      );
      input.value = turnWasSaved ? "" : text;
      setStatus(
        turnWasSaved
          ? "Arrêt demandé après l'enregistrement du tour."
          : "Génération arrêtée : aucun message de ce tour n'a été enregistré.",
      );
    } else {
      input.value = text;
      await loadMessages();
      setStatus(error.message, text);
    }
  } finally {
    if (reader) await reader.cancel().catch(() => {});
    activeController = null;
    sendButton.disabled = false;
    stopButton.hidden = true;
  }
}

document.querySelector("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (text) sendMessage(text);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    document.querySelector("#composer").requestSubmit();
  }
});

document.querySelector("#new-conversation").addEventListener("click", () => {
  createConversation().catch((error) => setStatus(error.message));
});

document.querySelector("#add-note").addEventListener("click", async () => {
  const content = input.value.trim();
  if (!content || activeController) return;
  try {
    if (!conversationId) await createConversation();
    const response = await fetch(`/conversations/${conversationId}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Impossible d'enregistrer cette note.");
    input.value = "";
    await loadMessages();
    await refreshConversations();
  } catch (error) {
    setStatus(error.message);
  }
});

stopButton.addEventListener("click", () => activeController?.abort());

Promise.all([loadModels(), refreshConversations()])
  .then(async ([, conversations]) => {
    if (conversations.length) await selectConversation(conversations[0].id);
    else await createConversation();
  })
  .catch((error) => setStatus(error.message));

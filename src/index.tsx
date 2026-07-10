import {
  ButtonItem,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  Router,
  TextField,
  showModal,
  staticClasses
} from "@decky/ui";
import {
  addEventListener,
  removeEventListener,
  callable,
  definePlugin,
  toaster,
} from "@decky/api"
import { useEffect, useState } from "react";
import { FaShip } from "react-icons/fa";
import ReactMarkdown from "react-markdown";

// Decky callable bridge: calls Plugin.send_message in main.py.
const sendMessageToBackend = callable<[text: string], void>("send_message");
const getChatHistory = callable<[], HistoryEntry[]>("get_chat_history");
const clearChatHistory = callable<[], void>("clear_chat_history");
const setApiKey = callable<[api_key: string], void>("set_api_key");

// Chat rows shown in the quick access panel.
type ChatMessage = {
  id: number;
  source: "local" | "backend";
  text: string;
  responseTimeMs?: number;
};

type HistoryEntry = {
  role: string;
  content: string;
};

const MESSAGE_FONT_SIZE = "11px";
const MESSAGE_LABEL_SIZE = "8px";

let nextMessageId = 0;
let draftCache = "";
let chatMessages: ChatMessage[] = [];
let historyHydrated = false;
let pendingRequests = 0;
const chatSubscribers = new Set<(messages: ChatMessage[]) => void>();
const waitingSubscribers = new Set<(waiting: boolean) => void>();

// Centralized message append so updates still work even if Content temporarily remounts.
const setWaitingFromPending = () => {
  const isWaiting = pendingRequests > 0;
  for (const notify of waitingSubscribers) {
    notify(isWaiting);
  }
};

const appendMessage = (source: ChatMessage["source"], text: string, responseTimeMs?: number) => {
  nextMessageId += 1;
  chatMessages = [...chatMessages, { id: nextMessageId, source, text, responseTimeMs }];
  for (const notify of chatSubscribers) {
    notify(chatMessages);
  }
};

const replaceMessages = (messages: ChatMessage[]) => {
  chatMessages = messages;
  nextMessageId = messages.reduce((maxId, message) => Math.max(maxId, message.id), 0);
  for (const notify of chatSubscribers) {
    notify(chatMessages);
  }
};

const getCurrentGameName = () => {
  const running = Router.MainRunningApp ?? Router.RunningApps[0];
  return running?.display_name;
};

// React hook that subscribes Content to centralized chat state.
const useChatMessages = () => {
  const [messages, setMessages] = useState<ChatMessage[]>(chatMessages);

  useEffect(() => {
    chatSubscribers.add(setMessages);
    return () => {
      chatSubscribers.delete(setMessages);
    };
  }, []);

  return messages;
};

const useIsWaiting = () => {
  const [isWaiting, setIsWaiting] = useState(pendingRequests > 0);

  useEffect(() => {
    waitingSubscribers.add(setIsWaiting);
    return () => {
      waitingSubscribers.delete(setIsWaiting);
    };
  }, []);

  return isWaiting;
};

// Props used by the popup composer modal.
type ComposeMessageModalProps = {
  initialText: string;
  onDraftChange: (text: string) => void;
  onSend: (text: string) => Promise<void>;
  onRequestClose: () => void;
  currentGameName?: string;
  isWaiting: boolean;
};

// Modal input UI. This is used so typing can happen in a popup above the side panel/keyboard.
function ComposeMessageModal({ initialText, onDraftChange, onSend, onRequestClose, currentGameName, isWaiting }: ComposeMessageModalProps) {
  // Local modal field state starts from the latest draft from the panel.
  const [text, setText] = useState(initialText);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const isSendDisabled = isWaiting || isSubmitting;

  const submitMessage = async () => {
    if (isSendDisabled) {
      return;
    }

    setIsSubmitting(true);
    try {
      await onSend(text);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <ModalRoot
      strTitle="Compose message"
      closeModal={onRequestClose}
      onCancel={onRequestClose}
      bDisableBackgroundDismiss
      bHideCloseIcon={false}
    >
      <PanelSection>
        {currentGameName ? (
          <PanelSectionRow>
            <div style={{ width: "100%", opacity: 0.75, fontSize: "11px" }}>
              Current game: {currentGameName}
            </div>
          </PanelSectionRow>
        ) : null}

        <PanelSectionRow>
          <TextField
            label="Message"
            value={text}
            // Focus the field immediately when the modal opens.
            focusOnMount
            onChange={(event) => {
              const nextText = event.target.value;
              // Keep modal-local state and panel draft in sync.
              setText(nextText);
              onDraftChange(nextText);
            }}
            onKeyDown={(event) => {
              // Enter sends through the same pipeline as the button.
              if (event.key === "Enter") {
                void submitMessage();
              }
            }}
            bShowClearAction
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem layout="below" disabled={isSendDisabled} onClick={() => void submitMessage()}>
            {isWaiting ? "Waiting for response..." : "Ask Assistant"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </ModalRoot>
  );
}

function SettingsModal({ onRequestClose }: { onRequestClose: () => void }) {
  const [apiKey, setApiKeyValue] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  const saveKey = async () => {
    setIsSubmitting(true);
    try {
      await setApiKey(apiKey);
      toaster.toast({ title: "Success", body: "API Key updated successfully." });
      onRequestClose();
    } catch (e) {
      toaster.toast({ title: "Error", body: String(e) });
      setIsSubmitting(false);
    }
  };

  return (
    <ModalRoot
      strTitle="Settings"
      closeModal={onRequestClose}
      onCancel={onRequestClose}
    >
      <PanelSection>
        <PanelSectionRow>
          <div style={{ opacity: 0.8, fontSize: "12px", marginBottom: "8px" }}>
            Enter your Gemini API Key here. You can still use the .env file instead.
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <TextField
            label="Gemini API Key"
            value={apiKey}
            onChange={(e) => setApiKeyValue(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={isSubmitting || !apiKey.trim()}
            onClick={() => void saveKey()}
          >
            Save API Key
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </ModalRoot>
  );
}

function Content() {
  // Draft is preserved between modal opens so user text is not lost.
  const [draft, setDraftState] = useState(draftCache);
  // Message list for the chat window in quick access.
  const messages = useChatMessages();
  const isWaiting = useIsWaiting();
  const [currentGameName, setCurrentGameName] = useState<string | undefined>(getCurrentGameName());
  const [thinkingDots, setThinkingDots] = useState(".");

  const setDraft = (value: string) => {
    draftCache = value;
    setDraftState(value);
  };

  useEffect(() => {
    if (historyHydrated) {
      return;
    }

    historyHydrated = true;
    void getChatHistory().then((history) => {
      const hydrated = history
        .filter((entry) => typeof entry?.role === "string" && typeof entry?.content === "string")
        .map((entry, index) => ({
          id: index + 1,
          source: entry.role === "assistant" ? "backend" as const : "local" as const,
          text: entry.content,
        }));

      replaceMessages(hydrated);
    }).catch((error) => {
      toaster.toast({
        title: "History load failed",
        body: String(error),
      });
    });
  }, []);

  useEffect(() => {
    const steamClient = (window as unknown as { SteamClient?: { GameSessions?: { RegisterForAppLifetimeNotifications?: (callback: () => void) => { unregister: () => void } } } }).SteamClient;
    const registration = steamClient?.GameSessions?.RegisterForAppLifetimeNotifications?.(() => {
      setCurrentGameName(getCurrentGameName());
    });

    return () => {
      registration?.unregister?.();
    };
  }, []);

  useEffect(() => {
    if (!isWaiting) {
      setThinkingDots(".");
      return;
    }

    const timer = window.setInterval(() => {
      setThinkingDots((prev) => (prev.length >= 3 ? "." : `${prev}.`));
    }, 300);

    return () => {
      window.clearInterval(timer);
    };
  }, [isWaiting]);

  const openComposeModal = () => {
    let modal: ReturnType<typeof showModal> | undefined;

    const prefill = currentGameName ? `In ${currentGameName} how do i ` : "";
    const initialDraft = draft.trim().length ? draft : prefill;

    if (!draft.trim().length && initialDraft) {
      setDraft(initialDraft);
    }

    // Open popup composer and wire send/close handlers.
    modal = showModal(
      <ComposeMessageModal
        initialText={initialDraft}
        currentGameName={currentGameName}
        isWaiting={isWaiting}
        onDraftChange={setDraft}
        onRequestClose={() => modal?.Close()}
        onSend={async (text) => {
          const trimmed = text.trim();
          // Ignore empty/whitespace-only submissions.
          if (!trimmed.length) {
            return;
          }

          if (pendingRequests > 0) {
            toaster.toast({
              title: "Please wait",
              body: "You already have a question in progress.",
            });
            return;
          }

          // Show user input immediately in the chat window.
          appendMessage("local", trimmed);
          // Keep last message text available as the next modal default.
          setDraft(trimmed);

          // Return to the plugin view immediately after submit.
          modal?.Close();

          // Send to Python backend in the background so the modal can close immediately.
          pendingRequests += 1;
          setWaitingFromPending();
          sendMessageToBackend(trimmed).catch((error) => {
            pendingRequests = Math.max(0, pendingRequests - 1);
            setWaitingFromPending();
            toaster.toast({
              title: "Send failed",
              body: String(error),
            });
          });
        }}
      />,
      undefined,
      {
        strTitle: "Compose message",
        bNeverPopOut: true,
        popupWidth: 720,
        popupHeight: 260,
      }
    );
  };

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <PanelSection>
        {currentGameName ? (
          <PanelSectionRow>
            <div style={{ width: "100%", opacity: 0.75, fontSize: "11px" }}>
              Playing: {currentGameName}
            </div>
          </PanelSectionRow>
        ) : null}

        <PanelSectionRow>
          {/* In-panel chat window that shows local messages and backend echoes. */}
          <div
            style={{
              width: "calc(100% - 12px)",
              minHeight: "calc(58vh - 12px)",
              maxHeight: "calc(58vh - 12px)",
              overflowY: "auto",
              border: "1px solid rgba(255, 255, 255, 0.15)",
              borderRadius: "8px",
              padding: "8px",
              display: "flex",
              flexDirection: "column",
              gap: "6px",
              margin: "0 auto",
            }}
          >
            {messages.length === 0 ? (
              <div style={{ opacity: 0.7, fontSize: MESSAGE_FONT_SIZE }}>Send a message to start the chat.</div>
            ) : (
              messages.map((message) => (
                <div
                  key={message.id}
                  style={{
                    background: message.source === "local" ? "rgba(0, 161, 255, 0.2)" : "rgba(124, 252, 0, 0.15)",
                    borderRadius: "6px",
                    padding: "6px 8px",
                  }}
                >
                  <div style={{ fontSize: MESSAGE_LABEL_SIZE, opacity: 0.7, marginBottom: "2px" }}>
                    {message.source === "local"
                      ? "You"
                      : message.responseTimeMs !== undefined
                        ? `Deck Muse (${(message.responseTimeMs / 1000).toFixed(2)} s)`
                        : "Deck Muse"}
                  </div>
                  {message.source === "backend" ? (
                    <div style={{ fontSize: MESSAGE_FONT_SIZE }}>
                      <ReactMarkdown>{message.text}</ReactMarkdown>
                    </div>
                  ) : (
                    <div style={{ fontSize: MESSAGE_FONT_SIZE }}>{message.text}</div>
                  )}
                </div>
              ))
            )}

            {isWaiting ? (
              <div
                style={{
                  background: "rgba(124, 252, 0, 0.12)",
                  borderRadius: "6px",
                  padding: "6px 8px",
                  fontSize: MESSAGE_FONT_SIZE,
                  opacity: 0.9,
                }}
              >
                Deck Muse is thinking{thinkingDots}
              </div>
            ) : null}
          </div>
        </PanelSectionRow>

        <PanelSectionRow>
          {/* Opens modal text entry so input is usable with the on-screen keyboard. */}
          <ButtonItem layout="below" disabled={isWaiting} onClick={openComposeModal}>
            {isWaiting ? "Waiting for response..." : "Type a question"}
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ width: "100%", opacity: 0.7, fontSize: "11px" }}>
            You can send one question at a time. Ask buttons are disabled until Deck Muse replies.
          </div>
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              await clearChatHistory();
              replaceMessages([]);
            }}
          >
            Clear chat history
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              let modal: ReturnType<typeof showModal> | undefined;
              modal = showModal(<SettingsModal onRequestClose={() => modal?.Close()} />);
            }}
          >
            Settings
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </div>
  );
};

export default definePlugin(() => {
  console.log("Template plugin initializing, this is called once on frontend startup")

  // Listen once at plugin scope so backend events are handled even if Content remounts.
  const listener = addEventListener<[text: string, responseTimeMs: number]>("chat_message", (text, responseTimeMs) => {
    appendMessage("backend", text, responseTimeMs);
    pendingRequests = Math.max(0, pendingRequests - 1);
    setWaitingFromPending();
  });

  return {
    // The name shown in various decky menus
    name: "Deck Muse",
    // The element displayed at the top of your plugin's menu
    titleView: <div className={staticClasses.Title}>Deck Muse</div>,
    // The content of your plugin's menu
    content: <Content />,
    // The icon displayed in the plugin list
    icon: <FaShip />,
    // The function triggered when your plugin unloads
    onDismount() {
      console.log("Unloading")
      removeEventListener("chat_message", listener);
    },
  };
});

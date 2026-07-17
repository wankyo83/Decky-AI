import {
  ButtonItem,
  DialogButton,
  DropdownItem,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  Router,
  TextField,
  showModal,
  staticClasses
} from "@decky/ui";
import {
  callable,
  definePlugin,
  FileSelectionType,
  openFilePicker,
  toaster,
  useQuickAccessVisible,
} from "@decky/api"
import { useEffect, useRef, useState } from "react";
import { FaMicrophone, FaRobot, FaStop } from "react-icons/fa";
import ReactMarkdown from "react-markdown";

// Decky callable bridge: calls Plugin.send_message in main.py.
type BackendResponse = { text: string; response_time_ms: number };
const sendMessageToBackend = callable<[text: string, game_name: string, question_mode: string, session_id: string], BackendResponse>("send_message");
const getChatHistory = callable<[], HistoryEntry[]>("get_chat_history");
const clearChatHistory = callable<[], void>("clear_chat_history");
const openPanel = callable<[session_id: string], void>("open_panel");
const closePanel = callable<[session_id: string], void>("close_panel");
type VoiceStartResult = { ok: boolean; message: string };
type VoiceStopResult = { ok: boolean; text: string; message: string };
const startVoiceRecording = callable<[session_id: string], VoiceStartResult>("start_voice_recording");
const stopVoiceRecording = callable<[session_id: string, game_name: string], VoiceStopResult>("stop_voice_recording");
const cancelVoiceRecording = callable<[session_id: string], void>("cancel_voice_recording");
const analyzeGameScreen = callable<[text: string, game_name: string, puzzle_mode: boolean, session_id: string], BackendResponse>("analyze_game_screen");
type YouTubeChapter = { seconds: number; timestamp: string; title: string; generated?: boolean };
const getYouTubeChapters = callable<[video_id: string], YouTubeChapter[]>("get_youtube_chapters");
const cancelYouTubeChapters = callable<[], void>("cancel_youtube_chapters");
type ImportApiKeyResult = { ok: boolean; message: string };
const importApiKeyFromIni = callable<[file_path: string], ImportApiKeyResult>("import_api_key_from_ini");

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

type QuestionMode = "gemini35" | "gemini25" | "google";

const extractYouTubeVideos = (text: string) => {
  const matches = text.matchAll(/(?:youtube\.com\/watch\?(?:[^\s)]*&)?v=|youtu\.be\/)([A-Za-z0-9_-]{11})/g);
  return Array.from(new Set(Array.from(matches, (match) => match[1])));
};

const extractGoogleSearchUrls = (text: string) => {
  const matches = text.matchAll(/https:\/\/www\.google\.com\/search\?q=[^\s)]+/g);
  return Array.from(new Set(Array.from(matches, (match) => match[0])));
};

const openExternalUrl = (url: string) => {
  const client = (window as unknown as {
    SteamClient?: { System?: { OpenInSystemBrowser?: (url: string) => void } };
  }).SteamClient;
  if (client?.System?.OpenInSystemBrowser) {
    client.System.OpenInSystemBrowser(url);
  } else {
    window.open(url, "_blank", "noopener,noreferrer");
  }
};

function VideoPlayerModal({ videoId, onRequestClose }: { videoId: string; onRequestClose: () => void }) {
  const [chapters, setChapters] = useState<YouTubeChapter[]>([]);
  const [chaptersLoaded, setChaptersLoaded] = useState(false);
  const [startSeconds, setStartSeconds] = useState(0);
  const watchUrl = `https://www.youtube.com/watch?v=${videoId}`;
  const pageOrigin = window.location.origin.startsWith("http")
    ? window.location.origin
    : "https://steamloopback.host";
  const embedUrl = new URL(`https://www.youtube.com/embed/${videoId}`);
  embedUrl.searchParams.set("playsinline", "1");
  embedUrl.searchParams.set("controls", "1");
  embedUrl.searchParams.set("rel", "0");
  embedUrl.searchParams.set("hl", "ko");
  embedUrl.searchParams.set("origin", pageOrigin);
  embedUrl.searchParams.set("widget_referrer", pageOrigin);
  if (startSeconds > 0) {
    embedUrl.searchParams.set("start", String(startSeconds));
    embedUrl.searchParams.set("autoplay", "1");
  }

  useEffect(() => {
    let active = true;
    getYouTubeChapters(videoId)
      .then((result) => {
        if (active) setChapters(result ?? []);
      })
      .catch(() => {
        if (active) setChapters([]);
      })
      .finally(() => {
        if (active) setChaptersLoaded(true);
      });
    return () => {
      active = false;
      void cancelYouTubeChapters();
    };
  }, [videoId]);

  return (
    <ModalRoot strTitle="YouTube 공략 영상" closeModal={onRequestClose} onCancel={onRequestClose}>
      <div style={{ width: "100%", height: "430px", display: "flex", flexDirection: "column", gap: "8px" }}>
        <iframe
          key={`${videoId}-${startSeconds}`}
          title="YouTube walkthrough"
          src={embedUrl.toString()}
          style={{ width: "100%", flex: 1, border: 0, borderRadius: "8px" }}
          referrerPolicy="strict-origin-when-cross-origin"
          allow="accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture; web-share; fullscreen"
          loading="eager"
          allowFullScreen
        />
        <div style={{ maxHeight: "120px", overflowY: "auto", fontSize: "11px" }}>
          {!chaptersLoaded ? (
            <div style={{ opacity: 0.7 }}>영상 타임테이블을 불러오는 중...</div>
          ) : chapters.length > 0 ? (
            <>
              <div style={{ opacity: 0.75, marginBottom: "5px" }}>
                {chapters[0]?.generated
                  ? "AI 생성 타임테이블 · 자동 자막 기반이므로 시간이 약간 다를 수 있습니다."
                  : "영상 제작자가 제공한 타임테이블"}
              </div>
              {chapters.map((chapter) => (
                <DialogButton
                  key={`${chapter.seconds}-${chapter.title}`}
                  onClick={() => setStartSeconds(chapter.seconds)}
                  style={{ width: "100%", minHeight: "30px", marginBottom: "4px", textAlign: "left" }}
                >
                  {chapter.timestamp}　{chapter.title}
                </DialogButton>
              ))}
            </>
          ) : (
            <div style={{ opacity: 0.7 }}>공식 챕터와 분석 가능한 자막이 없어 타임테이블을 만들 수 없습니다.</div>
          )}
        </div>
        <div style={{ fontSize: "10px", opacity: 0.7 }}>
          영상 소유자가 외부 재생을 막았거나 Steam 웹뷰에서 재생되지 않으면 아래 버튼으로 여세요.
        </div>
        <ButtonItem layout="below" onClick={() => openExternalUrl(watchUrl)}>
          Steam 브라우저에서 열기
        </ButtonItem>
      </div>
    </ModalRoot>
  );
}

const MESSAGE_FONT_SIZE = "11px";
const MESSAGE_LABEL_SIZE = "8px";

let nextMessageId = 0;
let draftCache = "";
let questionModeCache: QuestionMode = "gemini35";
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
  initialMode: QuestionMode;
  onDraftChange: (text: string) => void;
  onModeChange: (mode: QuestionMode) => void;
  onSend: (text: string, mode: QuestionMode) => Promise<void>;
  onVoiceStart: () => Promise<VoiceStartResult>;
  onVoiceStop: () => Promise<VoiceStopResult>;
  onVoiceCancel: () => Promise<void>;
  onRequestClose: () => void;
  currentGameName?: string;
  isWaiting: boolean;
};

// Modal input UI. This is used so typing can happen in a popup above the side panel/keyboard.
function ComposeMessageModal({ initialText, initialMode, onDraftChange, onModeChange, onSend, onVoiceStart, onVoiceStop, onVoiceCancel, onRequestClose, currentGameName, isWaiting }: ComposeMessageModalProps) {
  // Local modal field state starts from the latest draft from the panel.
  const [text, setText] = useState(initialText);
  const [questionMode, setQuestionMode] = useState<QuestionMode>(initialMode);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isVoiceRecording, setIsVoiceRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const isSendDisabled = isWaiting || isSubmitting || isVoiceRecording || isTranscribing;

  const closeComposer = () => {
    if (isVoiceRecording) {
      void onVoiceCancel();
    }
    onRequestClose();
  };

  const toggleModalVoice = async () => {
    try {
      if (!isVoiceRecording) {
        const result = await onVoiceStart();
        if (!result.ok) {
          toaster.toast({ title: "음성 입력 실패", body: result.message, critical: true });
          appendMessage("backend", `음성 입력 실패: ${result.message}`);
          return;
        }
        setIsVoiceRecording(true);
        toaster.toast({ title: "음성 입력", body: "말씀하세요. 다시 누르면 글로 변환합니다." });
        return;
      }

      setIsVoiceRecording(false);
      setIsTranscribing(true);
      const result = await onVoiceStop();
      if (!result.ok) {
        toaster.toast({ title: "음성 입력 실패", body: result.message, critical: true });
        appendMessage("backend", `음성 입력 실패: ${result.message}`);
        return;
      }
      const transcript = result.text.trim();
      if (transcript) {
        setText(transcript);
        onDraftChange(transcript);
      }
    } catch (error) {
      setIsVoiceRecording(false);
      appendMessage("backend", `음성 입력 실패: ${String(error)}`);
      toaster.toast({ title: "음성 입력 실패", body: String(error) });
    } finally {
      setIsTranscribing(false);
    }
  };

  const submitMessage = async () => {
    if (isSendDisabled) {
      return;
    }

    setIsSubmitting(true);
    try {
      await onSend(text, questionMode);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <ModalRoot
      strTitle="AI와 대화하기"
      closeModal={closeComposer}
      onCancel={closeComposer}
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
          <DropdownItem
            label="질문 유형"
            description={questionMode === "gemini35"
              ? "최신 AI · 일반 대화와 게임 질문 · 웹 검색 없음"
              : questionMode === "gemini25"
                ? "Gemini 2.5 + Google 실시간 검색"
                : "Gemini API 없이 Google 검색 페이지 열기"}
            rgOptions={[
              { data: "gemini35", label: "3.5 Flash" },
              { data: "gemini25", label: "2.5 Flash" },
              { data: "google", label: "Google 검색" },
            ]}
            selectedOption={questionMode}
            onChange={(option) => {
              const nextMode = option.data as QuestionMode;
              setQuestionMode(nextMode);
              questionModeCache = nextMode;
              onModeChange(nextMode);
            }}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ width: "100%" }}>
            <div style={{ marginBottom: "6px", fontSize: "14px", fontWeight: 600 }}>
              {isTranscribing ? "음성을 글로 변환하는 중..." : "메시지 입력"}
            </div>
            <div style={{ width: "100%", height: "52px", display: "flex", gap: "8px", alignItems: "stretch" }}>
              <div style={{ flex: 1, minWidth: 0, height: "52px" }}>
              <TextField
                value={text}
                focusOnMount
                disabled={isVoiceRecording || isTranscribing}
                style={{ height: "52px" }}
                onChange={(event) => {
                  const nextText = event.target.value;
                  setText(nextText);
                  onDraftChange(nextText);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    void submitMessage();
                  }
                }}
                bShowClearAction
              />
              </div>
              <DialogButton
                aria-label={isVoiceRecording ? "음성 입력 중지" : "음성 입력 시작"}
                disabled={isWaiting || isSubmitting || isTranscribing}
                onClick={() => void toggleModalVoice()}
                style={{ minWidth: "52px", width: "52px", minHeight: "52px", height: "52px", padding: 0, margin: 0 }}
              >
                {isVoiceRecording ? <FaStop color="#ff6b6b" /> : <FaMicrophone />}
              </DialogButton>
            </div>
          </div>
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem layout="below" disabled={isSendDisabled} onClick={() => void submitMessage()}>
            {isWaiting ? "답변을 기다리는 중..." : "질문 보내기"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </ModalRoot>
  );
}

function SettingsModal({ onRequestClose }: { onRequestClose: () => void }) {
  const [isSubmitting, setIsSubmitting] = useState(false);

  const importKey = async () => {
    setIsSubmitting(true);
    try {
      const selected = await openFilePicker(
        FileSelectionType.FILE,
        "/home/deck/Downloads",
        true,
        false,
        (file) => file.name.toLowerCase().endsWith(".ini"),
        ["ini"],
        false,
        false,
        1,
      );
      const selectedPath = selected.realpath || selected.path;
      const result = await importApiKeyFromIni(selectedPath);
      if (!result.ok) {
        toaster.toast({ title: "API 키 불러오기 실패", body: result.message, critical: true });
        return;
      }
      toaster.toast({ title: "API 키 불러오기 완료", body: result.message });
      onRequestClose();
    } catch (e) {
      // Closing the picker is not a Python/backend failure; show one concise message.
      toaster.toast({ title: "파일을 선택하지 않았습니다", body: "다시 시도하려면 불러오기 버튼을 누르세요." });
    } finally {
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
            파일 선택창에서 API 키가 들어 있는 .ini 파일을 선택하세요.
            <br /><br />
            파일 내용: GEMINI_API_KEY=발급받은_API_키
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={isSubmitting}
            onClick={() => void importKey()}
          >
            {isSubmitting ? "불러오는 중..." : "INI 파일 선택해서 API 키 불러오기"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </ModalRoot>
  );
}

function Content() {
  const quickAccessVisible = useQuickAccessVisible();
  const panelSessionId = useRef(`decky-ai-${Date.now()}-${Math.random().toString(36).slice(2)}`).current;
  // Draft is preserved between modal opens so user text is not lost.
  const [draft, setDraftState] = useState(draftCache);
  // Message list for the chat window in quick access.
  const messages = useChatMessages();
  const isWaiting = useIsWaiting();
  const [currentGameName, setCurrentGameName] = useState<string | undefined>(getCurrentGameName());
  const [thinkingDots, setThinkingDots] = useState(".");
  const [isRecording, setIsRecording] = useState(false);
  const panelCloseTimer = useRef<number | undefined>(undefined);
  const panelTransitionUntil = useRef(0);

  const setDraft = (value: string) => {
    draftCache = value;
    setDraftState(value);
  };

  useEffect(() => {
    // A Decky modal temporarily reports Quick Access as hidden. Do not let that
    // transition cancel a request that was just submitted from the composer.
    if (panelCloseTimer.current !== undefined) {
      window.clearTimeout(panelCloseTimer.current);
      panelCloseTimer.current = undefined;
    }

    if (quickAccessVisible) {
      void openPanel(panelSessionId);
    } else {
      setIsRecording(false);
      const transitionDelay = Math.max(
        750,
        panelTransitionUntil.current - Date.now(),
      );
      panelCloseTimer.current = window.setTimeout(() => {
        panelCloseTimer.current = undefined;
        void closePanel(panelSessionId);
      }, transitionDelay);
    }

    return () => {
      if (panelCloseTimer.current !== undefined) {
        window.clearTimeout(panelCloseTimer.current);
        panelCloseTimer.current = undefined;
      }
    };
  }, [quickAccessVisible, panelSessionId]);

  useEffect(() => () => {
    if (panelCloseTimer.current !== undefined) {
      window.clearTimeout(panelCloseTimer.current);
    }
    pendingRequests = 0;
    setWaitingFromPending();
    void closePanel(panelSessionId);
  }, [panelSessionId]);

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

  const openComposeModal = (overrideDraft?: string) => {
    let modal: ReturnType<typeof showModal> | undefined;

    const prefill = "";
    const initialDraft = overrideDraft ?? (draft.trim().length ? draft : prefill);

    if (!draft.trim().length && initialDraft) {
      setDraft(initialDraft);
    }

    // Open popup composer and wire send/close handlers.
    modal = showModal(
      <ComposeMessageModal
        initialText={initialDraft}
        initialMode={questionModeCache}
        currentGameName={currentGameName}
        isWaiting={isWaiting}
        onDraftChange={setDraft}
        onModeChange={(mode) => {
          questionModeCache = mode;
        }}
        onVoiceStart={async () => {
          if (pendingRequests > 0) {
            throw new Error("다른 AI 요청이 진행 중입니다.");
          }
          const result = await startVoiceRecording(panelSessionId);
          setIsRecording(result.ok);
          return result;
        }}
        onVoiceStop={async () => {
          try {
            return await stopVoiceRecording(panelSessionId, currentGameName ?? "");
          } finally {
            setIsRecording(false);
          }
        }}
        onVoiceCancel={async () => {
          try {
            await cancelVoiceRecording(panelSessionId);
          } finally {
            setIsRecording(false);
          }
        }}
        onRequestClose={() => modal?.Close()}
        onSend={async (text, questionMode) => {
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

          // Closing a Decky modal briefly reports Quick Access as hidden. Give
          // that UI transition time to settle before a hidden state may cancel
          // the backend request.
          panelTransitionUntil.current = Date.now() + 2000;
          if (panelCloseTimer.current !== undefined) {
            window.clearTimeout(panelCloseTimer.current);
            panelCloseTimer.current = undefined;
          }

          // Return to the plugin view immediately after submit and explicitly
          // reaffirm this session before starting the request.
          modal?.Close();
          void openPanel(panelSessionId);

          // Send to Python backend in the background so the modal can close immediately.
          pendingRequests += 1;
          setWaitingFromPending();
          sendMessageToBackend(trimmed, currentGameName ?? "", questionMode, panelSessionId)
            .then((result) => {
              if (result?.text) {
                appendMessage("backend", result.text, result.response_time_ms);
              }
            })
            .catch((error) => {
              appendMessage("backend", `질문을 보내지 못했습니다: ${String(error)}`);
              toaster.toast({
                title: "질문 전송 실패",
                body: String(error),
              });
            })
            .finally(() => {
              // Backend success, error, early return, and cancellation all clear thinking.
              pendingRequests = Math.max(0, pendingRequests - 1);
              setWaitingFromPending();
            });
        }}
      />,
      undefined,
      {
        strTitle: "AI와 대화하기",
        bNeverPopOut: true,
        popupWidth: 720,
        popupHeight: 340,
      }
    );
  };

  const runScreenAnalysis = (puzzleMode: boolean) => {
    if (pendingRequests > 0 || isRecording) {
      toaster.toast({ title: "잠시 기다려 주세요", body: "다른 작업이 진행 중입니다." });
      return;
    }
    const question = puzzleMode
      ? `실행 중인 게임(${currentGameName ?? "알 수 없음"})의 현재 화면과 관련된 YouTube 공략을 찾아줘.`
      : "현재 화면을 분석해줘.";
    appendMessage("local", `[화면 분석] ${question}`);
    pendingRequests += 1;
    setWaitingFromPending();
    analyzeGameScreen(question, currentGameName ?? "", puzzleMode, panelSessionId)
      .then((result) => {
        if (result?.text) {
          appendMessage("backend", result.text, result.response_time_ms);
        }
      })
      .catch((error) => {
        appendMessage("backend", `화면 분석을 시작하지 못했습니다: ${String(error)}`);
        toaster.toast({ title: "화면 분석 실패", body: String(error) });
      })
      .finally(() => {
        pendingRequests = Math.max(0, pendingRequests - 1);
        setWaitingFromPending();
      });
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
                        ? `Decky AI (${(message.responseTimeMs / 1000).toFixed(2)} s)`
                        : "Decky AI"}
                  </div>
                  {message.source === "backend" ? (
                    <div style={{ fontSize: MESSAGE_FONT_SIZE }}>
                      <ReactMarkdown>{message.text}</ReactMarkdown>
                      {extractYouTubeVideos(message.text).map((videoId, index) => (
                        <ButtonItem
                          key={videoId}
                          layout="below"
                          onClick={() => {
                            let modal: ReturnType<typeof showModal> | undefined;
                            modal = showModal(
                              <VideoPlayerModal videoId={videoId} onRequestClose={() => modal?.Close()} />,
                              undefined,
                              { strTitle: "YouTube 공략", bNeverPopOut: true, popupWidth: 800, popupHeight: 520 },
                            );
                          }}
                        >
                          공략 영상 {index + 1} 재생
                        </ButtonItem>
                      ))}
                      {extractGoogleSearchUrls(message.text).map((url) => (
                        <ButtonItem key={url} layout="below" onClick={() => openExternalUrl(url)}>
                          Google 검색 결과 열기
                        </ButtonItem>
                      ))}
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
                Decky AI is thinking{thinkingDots}
              </div>
            ) : null}
          </div>
        </PanelSectionRow>

        <PanelSectionRow>
          {/* Opens modal text entry so input is usable with the on-screen keyboard. */}
          <ButtonItem layout="below" disabled={isWaiting} onClick={() => openComposeModal()}>
            {isWaiting ? "답변을 기다리는 중..." : "AI 대화하기"}
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem layout="below" disabled={isWaiting || isRecording} onClick={() => runScreenAnalysis(true)}>
            YouTube 공략 찾기
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ width: "100%", opacity: 0.68, fontSize: "10px" }}>
            현재 게임 이름과 자동 캡처 화면을 함께 분석해 YouTube 공략을 찾습니다.
          </div>
        </PanelSectionRow>

        <PanelSectionRow>
          <div style={{ width: "100%", opacity: 0.7, fontSize: "11px" }}>
            텍스트 질문은 답변이 끝날 때까지 유지되며, 화면 캡처와 음성 녹음은 창을 닫으면 중단됩니다.
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

        <PanelSectionRow>
          <div style={{ width: "100%", opacity: 0.45, fontSize: "9px", textAlign: "right" }}>
            Decky AI v0.1.4
          </div>
        </PanelSectionRow>
      </PanelSection>
    </div>
  );
};

export default definePlugin(() => {
  console.log("Template plugin initializing, this is called once on frontend startup")

  return {
    // The name shown in various decky menus
    name: "Decky AI",
    // The element displayed at the top of your plugin's menu
    titleView: <div className={staticClasses.Title}>Decky AI</div>,
    // The content of your plugin's menu
    content: <Content />,
    // The icon displayed in the plugin list
    icon: <FaRobot />,
    // The function triggered when your plugin unloads
    onDismount() {
      console.log("Unloading")
    },
  };
});

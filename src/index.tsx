// DeckDrop — Decky Loader panel. Receiving and saving happen in main.py; this asks for approval and shows progress.

import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  Field,
  Focusable,
  PanelSection,
  PanelSectionRow,
  ProgressBarWithInfo,
  ToggleField,
  showModal,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, openFilePicker, removeEventListener, toaster } from "@decky/api";
import { useEffect, useState } from "react";
import { FaFileDownload } from "react-icons/fa";

type Peer = { id: string; name: string; kind: string; ip: string };
type Offer = { id: number; from: string; count: number; size: number; names: string[]; folders: number; time: number; expires: number; folder: string };
type Transfer = {
  id: number; from: string; count: number; size: number; received: number; index: number; current: string; elapsed: number;
  cancelling: boolean; paused: boolean; paused_by: string; can_pause: boolean;
};
type Item = { id: number; from: string; count: number; size: number; saved: number; received: number; status: string; message: string; names: string[]; time: number };
type Settings = { enabled: boolean; notify: boolean };
type State = {
  me: { name: string; ips: string[]; port: number };
  folder: string;          // where files go (full path)
  default_folder: string;
  peers: Peer[];
  offers: Offer[];
  transfers: Transfer[];
  history: Item[];
  settings: Settings;
  listening: boolean;
  status: string;
  error: boolean;
};
type Done = { id: number; from: string; status: string; message: string; saved: number; count: number; size: number; folder: string; notify: boolean };

const getState = callable<[], State>("get_state");
const respondOffer = callable<[id: number, accept: boolean], boolean>("respond_offer");
const cancelTransfer = callable<[id: number], boolean>("cancel_transfer");
const pauseTransfer = callable<[id: number, paused: boolean], boolean>("pause_transfer");
const clearHistory = callable<[], void>("clear_history");
const setSetting = callable<[key: string, value: boolean], void>("set_setting");
const setFolder = callable<[path: string], { ok: boolean; message: string }>("set_folder");
const refreshNow = callable<[], void>("refresh");

// FileSelectionType.FOLDER (a const enum in @decky/api's typings, so it can't be imported as a value here).
const PICK_FOLDER = 1 as unknown as Parameters<typeof openFilePicker>[0];

const muted = "#8b929a";

function formatSize(n: number) {
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n} bytes` : `${n.toFixed(1)} ${units[i]}`;
}

function plural(n: number, word: string) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

function formatWhen(epochSeconds: number) {
  const t = new Date(epochSeconds * 1000);
  if (t.toDateString() === new Date().toDateString())
    return t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return t.toLocaleDateString([], { day: "numeric", month: "short" });
}

function formatDuration(seconds: number) {
  seconds = Math.max(0, Math.round(seconds));
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return m > 0 ? `${m}:${String(s).padStart(2, "0")}` : `${s} s`;
}

// "3 files (photo.jpg, notes.txt…)" — the top-level names the PC chose, so you know what you're accepting.
function describeOffer(o: { count: number; names: string[]; folders: number }) {
  const shown = o.names.slice(0, 3).join(", ") + (o.names.length > 3 ? "…" : "");
  const what = o.folders > 0 ? `${plural(o.count, "file")} in ${plural(o.folders, "folder")}` : plural(o.count, "file");
  return `${what}: ${shown}`;
}

// Approval prompts that are open, by offer id, so answering in the panel (or the PC giving up) closes them.
const prompts = new Map<number, { Close: () => void }>();
const answered = new Set<number>();

async function answer(id: number, accept: boolean) {
  if (answered.has(id)) return;
  answered.add(id);
  try { await respondOffer(id, accept); } catch (e) { console.error("DeckDrop", e); }
  closePrompt(id);
}

function closePrompt(id: number) {
  const p = prompts.get(id);
  prompts.delete(id);
  try { p?.Close(); } catch { /* already closed */ }
}

function showPrompt(o: Offer) {
  const handle = showModal(
    <ConfirmModal
      strTitle={`Receive files from ${o.from}?`}
      strDescription={
        <div style={{ lineHeight: "1.5em" }}>
          <div style={{ wordBreak: "break-word" }}>{describeOffer(o)}</div>
          <div style={{ color: muted, marginTop: "8px", wordBreak: "break-word" }}>{formatSize(o.size)} · saved to {o.folder}</div>
        </div>
      }
      strOKButtonText="Accept"
      strCancelButtonText="Decline"
      onOK={() => answer(o.id, true)}
      onCancel={() => answer(o.id, false)}
    />,
  );
  prompts.set(o.id, handle);
}

function Note({ text, error }: { text: string; error?: boolean }) {
  if (!text) return null;
  return (
    <PanelSectionRow>
      <div style={{ fontSize: "12px", lineHeight: "16px", color: error ? "#ff7068" : muted, padding: "2px 0 6px", wordBreak: "break-word" }}>{text}</div>
    </PanelSectionRow>
  );
}

function PeerList({ peers }: { peers: Peer[] }) {
  return (
    <PanelSectionRow>
      <div style={{ padding: "4px 0 8px" }}>
        {peers.map((p) => (
          <div key={p.id} style={{ display: "flex", alignItems: "center", gap: "8px", lineHeight: "22px" }}>
            <div style={{ width: "8px", height: "8px", borderRadius: "4px", background: "#46d478", flexShrink: 0 }} />
            <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.name}</div>
            <div style={{ fontSize: "12px", color: muted, marginLeft: "auto" }}>{p.ip}</div>
          </div>
        ))}
      </div>
    </PanelSectionRow>
  );
}

function TransferRow({ t }: { t: Transfer }) {
  const fraction = t.size > 0 ? t.received / t.size : 1;
  const speed = t.elapsed > 0.5 ? t.received / t.elapsed : 0;
  const left = speed > 0 ? (t.size - t.received) / speed : 0;
  const operation = t.cancelling ? "Cancelling…"
    : t.paused ? `Paused on ${t.paused_by === "deck" ? "this Deck" : "the PC"}`
    : `File ${Math.min(t.index + 1, t.count)} of ${t.count}: ${t.current}`;
  // The PC can always pause; pausing from here needs a PC app that understands it (1.2 or newer).
  const canToggle = t.can_pause || (t.paused && t.paused_by === "deck");
  return (
    <>
      <PanelSectionRow>
        <ProgressBarWithInfo
          label={`From ${t.from}`}
          nProgress={fraction * 100}
          sOperationText={operation}
          description={`${formatSize(t.received)} of ${formatSize(t.size)}${speed > 0 && !t.paused ? ` · ${formatSize(speed)}/s · ${formatDuration(left)} left` : ""}`}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <Focusable style={{ display: "flex", gap: "8px" }} flow-children="horizontal">
          {canToggle && (
            <DialogButton style={{ flex: 1, minWidth: 0 }} disabled={t.cancelling} onClick={() => pauseTransfer(t.id, !t.paused)}>
              {t.paused ? "Resume" : "Pause"}
            </DialogButton>
          )}
          <DialogButton style={{ flex: 1, minWidth: 0 }} disabled={t.cancelling} onClick={() => cancelTransfer(t.id)}>Cancel</DialogButton>
        </Focusable>
      </PanelSectionRow>
    </>
  );
}

const STATUS_COLOR: Record<string, string> = { done: "#46d478", declined: muted, expired: muted, cancelled: muted, failed: "#ff7068" };

function Content() {
  const [s, setS] = useState<State | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [folderNote, setFolderNote] = useState<{ text: string; error: boolean } | null>(null);

  const refresh = async () => {
    try {
      setS(await getState());
      setBackendError(null);
    } catch (e) {
      console.error("DeckDrop", e);
      setBackendError(String(e));
    }
  };

  useEffect(() => {
    refreshNow().catch((e) => console.error("DeckDrop", e));
    refresh();
    const timer = setInterval(refresh, 700);
    const onEvent = () => { refresh(); };
    const l1 = addEventListener("deckdrop_offer", onEvent);
    const l2 = addEventListener("deckdrop_done", onEvent);
    const l3 = addEventListener("deckdrop_closed", onEvent);
    return () => {
      clearInterval(timer);
      removeEventListener("deckdrop_offer", l1);
      removeEventListener("deckdrop_done", l2);
      removeEventListener("deckdrop_closed", l3);
    };
  }, []);

  if (!s) {
    return (
      <PanelSection>
        <Note
          text={backendError ? "The plugin's backend isn't responding: " + backendError + ". Check ~/homebrew/logs/DeckDrop/." : "Loading…"}
          error={!!backendError}
        />
      </PanelSection>
    );
  }

  const save = async (key: keyof Settings, value: boolean) => { await setSetting(key, value); await refresh(); };
  const now = Date.now() / 1000;

  const changeFolder = async (path: string) => {
    try {
      const r = await setFolder(path);
      setFolderNote({ text: r.message, error: !r.ok });
    } catch (e) {
      setFolderNote({ text: String(e), error: true });
    }
    refresh();
  };
  const chooseFolder = async () => {
    let picked: { path: string; realpath: string };
    try {
      picked = await openFilePicker(PICK_FOLDER, s.folder, false, true);
    } catch {
      return;                        // closed without choosing
    }
    if (picked?.path) await changeFolder(picked.realpath || picked.path);
  };
  const isDefaultFolder = s.folder === s.default_folder;

  const power = (
    <PanelSection>
      <PanelSectionRow>
        <ToggleField
          label="DeckDrop"
          description={s.settings.enabled ? "Ready to receive files from your PC" : "Off: not on the network, nothing can be sent here"}
          checked={s.settings.enabled}
          onChange={(on) => save("enabled", on)}
        />
      </PanelSectionRow>
      {!s.listening && s.settings.enabled && <Note text={s.status || `Can't receive: port ${s.me.port} is busy.`} error />}
    </PanelSection>
  );
  if (!s.settings.enabled) return power;

  return (
    <>
      {power}

      {s.offers.length > 0 && (
        <PanelSection title="Waiting for you">
          {s.offers.map((o) => (
            <PanelSectionRow key={o.id}>
              <Field
                label={<span style={{ wordBreak: "break-word" }}>{o.from} wants to send {plural(o.count, "file")}</span>}
                description={`${describeOffer(o)} · ${formatSize(o.size)} · ${formatDuration(o.expires - now)} to answer`}
                childrenLayout="below"
              >
                <Focusable style={{ display: "flex", gap: "8px" }} flow-children="horizontal">
                  <DialogButton style={{ flex: 1, minWidth: 0 }} onClick={() => answer(o.id, true).then(refresh)}>Accept</DialogButton>
                  <DialogButton style={{ flex: 1, minWidth: 0 }} onClick={() => answer(o.id, false).then(refresh)}>Decline</DialogButton>
                </Focusable>
              </Field>
            </PanelSectionRow>
          ))}
        </PanelSection>
      )}

      {s.transfers.length > 0 && (
        <PanelSection title="Receiving">
          {s.transfers.map((t) => <TransferRow key={t.id} t={t} />)}
        </PanelSection>
      )}

      <PanelSection title="Received">
        {s.history.length === 0
          ? <Note text={`Files sent from your PC are saved to ${s.folder}. You're asked first every time.`} />
          : s.history.slice(0, 8).map((h) => (
            <PanelSectionRow key={h.id}>
              <Field
                label={<span style={{ wordBreak: "break-word" }}>{h.names.slice(0, 2).join(", ") + (h.names.length > 2 ? "…" : "")}</span>}
                description={
                  <span>
                    <span style={{ color: STATUS_COLOR[h.status] ?? muted }}>
                      {h.status === "done" ? `${plural(h.saved, "file")} saved` : h.status === "failed" && h.saved > 0 ? `${h.message} (${h.saved} of ${h.count} saved)` : h.message}
                    </span>
                    {` · ${formatSize(h.size)} · from ${h.from} · ${formatWhen(h.time)}`}
                  </span>
                }
              />
            </PanelSectionRow>
          ))}
        {s.history.length > 0 && (
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={async () => { await clearHistory(); refresh(); }}>Clear list</ButtonItem>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="PCs">
        {s.peers.length > 0
          ? <PeerList peers={s.peers} />
          : <Note text="Looking for your PC… Open DeckDrop on it, on the same network as this Deck." />}
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <Field
            label="Save files to"
            description={<span style={{ wordBreak: "break-all" }}>{s.folder}</span>}
            childrenLayout="below"
          >
            <Focusable style={{ display: "flex", gap: "8px" }} flow-children="horizontal">
              <DialogButton style={{ flex: 1, minWidth: 0 }} onClick={chooseFolder}>Choose folder</DialogButton>
              {!isDefaultFolder && (
                <DialogButton style={{ flex: 1, minWidth: 0 }} onClick={() => changeFolder("")}>Use default</DialogButton>
              )}
            </Focusable>
          </Field>
        </PanelSectionRow>
        {folderNote && <Note text={folderNote.text} error={folderNote.error} />}
        {s.transfers.length > 0 && <Note text="Transfers already under way finish in the folder they started in." />}
        <PanelSectionRow>
          <ToggleField
            label="Notifications"
            description="Show a notification when files arrive or someone wants to send some"
            checked={s.settings.notify}
            onChange={(on) => save("notify", on)}
          />
        </PanelSectionRow>
        <Note text={`This device: ${s.me.name}${s.me.ips.length ? " · " + s.me.ips.join(", ") : ""}.`} />
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // Registered for the plugin's lifetime so requests arriving while the menu is closed still pop up.
  const onOffer = addEventListener<[Offer & { notify: boolean }]>("deckdrop_offer", (o) => {
    showPrompt(o);
    if (o.notify)
      toaster.toast({ title: `${o.from} wants to send ${plural(o.count, "file")}`, body: `${formatSize(o.size)} · accept or decline in DeckDrop` });
  });
  const onClosed = addEventListener<[{ id: number }]>("deckdrop_closed", (c) => {
    answered.add(c.id);
    closePrompt(c.id);
  });
  const onDone = addEventListener<[Done]>("deckdrop_done", (d) => {
    if (!d.notify) return;
    if (d.status === "done")
      toaster.toast({ title: `Received ${plural(d.saved, "file")} from ${d.from}`, body: `${formatSize(d.size)} · saved to ${d.folder}` });
    else
      toaster.toast({ title: `Transfer from ${d.from} stopped`, body: d.message + (d.saved > 0 ? ` (${d.saved} of ${d.count} files saved)` : "") });
  });
  // Requests that arrived before the frontend loaded (e.g. right after Steam restarted) still need an answer.
  getState().then((s) => s.offers.forEach(showPrompt)).catch(() => {});
  return {
    name: "DeckDrop",
    titleView: <div className={staticClasses.Title}>DeckDrop</div>,
    content: <Content />,
    icon: <FaFileDownload />,
    onDismount() {
      removeEventListener("deckdrop_offer", onOffer);
      removeEventListener("deckdrop_closed", onClosed);
      removeEventListener("deckdrop_done", onDone);
      for (const id of Array.from(prompts.keys())) closePrompt(id);
    },
  };
});

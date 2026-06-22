// Vinyl Player — desktop widget for Übersicht
// ------------------------------------------------------------------
// Controls the local Vinyl Player server (vinyl_player.py):
//   • toggle the app on/off (starts/stops the background process)
//   • when on, shows the current track and play/pause/prev/next controls
//   • opens the player in the browser
// Themes: auto (follows system) / dark / light / transparent — cycle with
// the small circle button in the header. Choice is remembered.
//
// Install: copy/symlink this `.widget` folder into
//   ~/Library/Application Support/Übersicht/widgets/
// (see macos-widget/install.sh).

// `run` (shell exec) is a named export of the `uebersicht` module — it is NOT a
// global. Without this import, click handlers throw ReferenceError silently.
import { run } from "uebersicht";

// ─────────── config — edit these paths if your setup differs ───────────
const PY   = "/usr/bin/python3";
const APP  = "/Users/insideside/vk-music/vinyl_player.py";
const LOG  = "/Users/insideside/vk-music/logs/widget_player.log";
const HTTPS = "https://127.0.0.1:7656";
const HTTP  = "http://127.0.0.1:7656";

// ─────────── refresh & data command ───────────
export const refreshFrequency = 1500;

// Reports: whether the server process is alive, which protocol answers, and
// the current now-playing state (or null when the server is down).
export const command = `
P=$(/usr/bin/pgrep -f '[Pp]ython.*vinyl_player.py' | /usr/bin/head -1)
if [ -n "$P" ]; then R=true; else R=false; fi
PROTO=https
S=$(/usr/bin/curl -sk --max-time 1 ${HTTPS}/api/widget/state)
if [ -z "$S" ]; then PROTO=http; S=$(/usr/bin/curl -s --max-time 1 ${HTTP}/api/widget/state); fi
[ -z "$S" ] && S=null
/usr/bin/printf '{"running":%s,"proto":"%s","state":%s}' "$R" "$PROTO" "$S"
`;

// ─────────── actions (run = Übersicht shell helper) ───────────
// Launch in the app's normal mode: it auto-restores the saved LAN/WAN setting,
// so the player is reachable from other devices (e.g. iPhone) over the network.
// Enable LAN once from the app UI; on a fresh machine without it, this stays
// localhost-only. (vinyl_player.py still accepts --local to force localhost.)
// --no-browser: the toggle only starts the server; the player is opened
// separately (e.g. from the Safari "Add to Dock" web app), not as a browser tab.
const startApp = () =>
  run(`nohup "${PY}" "${APP}" --no-browser >> "${LOG}" 2>&1 &`);

const stopApp = () =>
  run(`/usr/bin/pkill -f '[Pp]ython.*vinyl_player.py'`);

const baseFor = (proto) => (proto === "http" ? HTTP : HTTPS);

const sendCmd = (proto, c) => {
  const b = baseFor(proto);
  return run(
    `/usr/bin/curl -sk -X POST ${b}/api/widget/command ` +
    `-H 'Content-Type: application/json' -d '{"cmd":"${c}"}'`
  );
};

const openBrowser = (proto) => run(`/usr/bin/open ${baseFor(proto)}`);

// ─────────── theme handling ───────────
const THEMES = ["auto", "dark", "light", "transparent"];
const readTheme = () => {
  try { return localStorage.getItem("vinylWidgetTheme") || "auto"; }
  catch (e) { return "auto"; }
};
const applyTheme = (t) => {
  try { localStorage.setItem("vinylWidgetTheme", t); } catch (e) {}
  const el = document.getElementById("vinyl-widget-root");
  if (el) el.className = "vw-root theme-" + t;
};
const cycleTheme = () => {
  const cur = readTheme();
  const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  applyTheme(next);
};

// ─────────── icons ───────────
const Icon = ({ d, size = 20 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
    <path d={d} />
  </svg>
);
const I_PREV = "M6 6h2v12H6zm3.5 6l8.5 6V6z";
const I_NEXT = "M16 6h2v12h-2zM6 18l8.5-6L6 6z";
const I_PLAY = "M8 5v14l11-7z";
const I_PAUSE = "M6 19h4V5H6zm8-14v14h4V5z";
const I_EXT = "M14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3zM19 19H5V5h7V3H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7h-2z";

// ─────────── render ───────────
export const render = ({ output }) => {
  let data = {};
  try { data = JSON.parse(output); } catch (e) { data = {}; }

  const running = !!data.running;
  const proto = data.proto || "https";
  const st = data.state || null;
  // "live" = a browser tab is actually open and reporting state recently,
  // so playback controls will be received.
  const live = !!st && st.age != null && st.age < 8;
  const playing = live && !!st.playing;
  const title = live ? (st.title || "") : "";
  const artist = live ? (st.artist || "") : "";

  const theme = readTheme();

  let trackLine, subLine;
  if (!running) {
    trackLine = "Выключено";
    subLine = "Нажмите, чтобы запустить";
  } else if (!live) {
    trackLine = "Плеер не открыт";
    subLine = "Откройте плеер в браузере";
  } else if (!title && !artist) {
    trackLine = "Готово к воспроизведению";
    subLine = "Трек не выбран";
  } else {
    trackLine = title || "Без названия";
    subLine = artist || "";
  }

  return (
    <div id="vinyl-widget-root" className={"vw-root theme-" + theme}>
      <div className="vw-header">
        <span className={"vw-dot " + (running ? "on" : "off")} />
        <span className="vw-name">Vinyl Player</span>
        <span
          className="vw-theme"
          title={"Тема: " + theme}
          onClick={cycleTheme}
        />
        <div
          className={"vw-toggle " + (running ? "on" : "off")}
          title={running ? "Выключить" : "Включить"}
          onClick={() => (running ? stopApp() : startApp())}
        >
          <span className="vw-knob" />
        </div>
      </div>

      <div className="vw-body">
        <div className="vw-track">
          <div className="vw-title">{trackLine}</div>
          <div className="vw-sub">{subLine}</div>
        </div>

        {running && live ? (
          <div className="vw-controls">
            <button className="vw-btn" title="Предыдущий"
              onClick={() => sendCmd(proto, "prev")}>
              <Icon d={I_PREV} />
            </button>
            <button className="vw-btn vw-play" title={playing ? "Пауза" : "Играть"}
              onClick={() => sendCmd(proto, "toggle")}>
              <Icon d={playing ? I_PAUSE : I_PLAY} size={24} />
            </button>
            <button className="vw-btn" title="Следующий"
              onClick={() => sendCmd(proto, "next")}>
              <Icon d={I_NEXT} />
            </button>
            <button className="vw-btn vw-open" title="Открыть в браузере"
              onClick={() => openBrowser(proto)}>
              <Icon d={I_EXT} size={18} />
            </button>
          </div>
        ) : running ? (
          <div className="vw-controls">
            <button className="vw-btn vw-open vw-wide" title="Открыть в браузере"
              onClick={() => openBrowser(proto)}>
              <Icon d={I_EXT} size={18} />
              <span style={{ marginLeft: 8 }}>Открыть плеер</span>
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
};

// ─────────── styles ───────────
// The exported className wraps everything; theme variables live on .vw-root.
export const className = `
  top: 40px;
  left: 40px;
  font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  -webkit-font-smoothing: antialiased;

  .vw-root {
    --bg: rgba(28, 28, 32, 0.92);
    --fg: #f2f2f5;
    --muted: rgba(242, 242, 245, 0.55);
    --accent: #e94560;
    --border: rgba(255, 255, 255, 0.10);
    --btn: rgba(255, 255, 255, 0.08);
    --btn-hover: rgba(255, 255, 255, 0.16);
    --shadow: 0 8px 30px rgba(0, 0, 0, 0.35);

    width: 270px;
    box-sizing: border-box;
    padding: 14px 16px 16px;
    border-radius: 16px;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid var(--border);
    box-shadow: var(--shadow);
    -webkit-backdrop-filter: blur(20px);
    backdrop-filter: blur(20px);
    user-select: none;
  }

  /* Light theme */
  .vw-root.theme-light {
    --bg: rgba(250, 250, 252, 0.95);
    --fg: #1c1c20;
    --muted: rgba(28, 28, 32, 0.5);
    --border: rgba(0, 0, 0, 0.08);
    --btn: rgba(0, 0, 0, 0.05);
    --btn-hover: rgba(0, 0, 0, 0.12);
    --shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
  }

  /* Transparent theme — minimal chrome over the wallpaper */
  .vw-root.theme-transparent {
    --bg: rgba(0, 0, 0, 0.18);
    --fg: #ffffff;
    --muted: rgba(255, 255, 255, 0.7);
    --border: rgba(255, 255, 255, 0.18);
    --btn: rgba(255, 255, 255, 0.12);
    --btn-hover: rgba(255, 255, 255, 0.25);
    --shadow: none;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5);
  }

  /* Auto — follow the system appearance */
  @media (prefers-color-scheme: light) {
    .vw-root.theme-auto {
      --bg: rgba(250, 250, 252, 0.95);
      --fg: #1c1c20;
      --muted: rgba(28, 28, 32, 0.5);
      --border: rgba(0, 0, 0, 0.08);
      --btn: rgba(0, 0, 0, 0.05);
      --btn-hover: rgba(0, 0, 0, 0.12);
      --shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
    }
  }

  .vw-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
  }
  .vw-dot {
    width: 8px; height: 8px; border-radius: 50%;
    flex: 0 0 auto;
  }
  .vw-dot.on  { background: #34c759; box-shadow: 0 0 8px rgba(52,199,89,0.8); }
  .vw-dot.off { background: var(--muted); }

  .vw-name {
    font-size: 13px; font-weight: 600; letter-spacing: 0.2px;
    flex: 1 1 auto;
  }

  .vw-theme {
    width: 14px; height: 14px; border-radius: 50%;
    flex: 0 0 auto; cursor: pointer;
    background: conic-gradient(var(--accent), var(--fg), var(--muted), var(--accent));
    border: 1px solid var(--border);
    opacity: 0.85;
  }
  .vw-theme:hover { opacity: 1; transform: scale(1.1); }

  .vw-toggle {
    width: 40px; height: 24px; border-radius: 13px;
    flex: 0 0 auto; cursor: pointer; position: relative;
    background: var(--btn); border: 1px solid var(--border);
    transition: background 0.2s;
  }
  .vw-toggle.on { background: var(--accent); border-color: transparent; }
  .vw-knob {
    position: absolute; top: 2px; left: 2px;
    width: 18px; height: 18px; border-radius: 50%;
    background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    transition: left 0.2s;
  }
  .vw-toggle.on .vw-knob { left: 19px; }

  .vw-track { margin-bottom: 12px; }
  .vw-title {
    font-size: 15px; font-weight: 600; line-height: 1.25;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .vw-sub {
    font-size: 12px; color: var(--muted); margin-top: 2px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    min-height: 15px;
  }

  .vw-controls {
    display: flex; align-items: center; gap: 8px;
  }
  .vw-btn {
    display: inline-flex; align-items: center; justify-content: center;
    border: none; cursor: pointer; color: var(--fg);
    background: var(--btn); border-radius: 10px;
    width: 38px; height: 38px; padding: 0;
    transition: background 0.15s, transform 0.1s;
  }
  .vw-btn:hover { background: var(--btn-hover); }
  .vw-btn:active { transform: scale(0.92); }
  .vw-play { background: var(--accent); color: #fff; width: 44px; height: 44px; }
  .vw-play:hover { background: var(--accent); filter: brightness(1.1); }
  .vw-open { margin-left: auto; }
  .vw-wide {
    width: auto; margin-left: 0; flex: 1 1 auto; padding: 0 14px;
    font-size: 13px; font-weight: 500; height: 40px;
  }
`;

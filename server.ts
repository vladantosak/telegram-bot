import express from "express";
import path from "path";
import { spawn, execSync, ChildProcess } from "child_process";
import { createServer as createViteServer } from "vite";

const app = express();
const PORT = 3000;

app.use(express.json());

// Helper to run python DB query safely without native node modules
function runPythonQuery(pythonCode: string): Promise<any> {
  return new Promise((resolve, reject) => {
    const py = spawn("python3", ["-c", pythonCode], { cwd: process.cwd() });
    let stdout = "";
    let stderr = "";
    // BUG FIX: spawn() emits "error" (e.g. ENOENT if python3 isn't on PATH) as a distinct
    // event from "close". An EventEmitter's unhandled "error" event throws and crashes the
    // whole Node process by default - every /api/workers /reports /objects call would have
    // taken the entire dashboard down with it if python3 were ever missing.
    py.on("error", (err) => reject(err));
    py.stdout.on("data", (data) => (stdout += data.toString()));
    py.stderr.on("data", (data) => (stderr += data.toString()));
    py.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr || `Python process exited with code ${code}`));
      } else {
        try {
          resolve(JSON.parse(stdout.trim()));
        } catch (e) {
          resolve({ raw: stdout.trim() });
        }
      }
    });
  });
}

// 1. Setup Python Dependencies on startup
console.log("Checking and installing Python dependencies...");
try {
  execSync(
    "python3 -m pip install --upgrade pip && python3 -m pip install python-telegram-bot groq openpyxl gspread google-auth cryptography",
    { stdio: "inherit" }
  );
  console.log("Python dependencies verified successfully.");
} catch (e) {
  console.error("Warning: Non-blocking error installing python packages:", e);
}

// 2. Spawn and manage the bot.py child process
let botProcess: ChildProcess | null = null;
let botStatus = "Stopped";
let botLogs: string[] = [];

function startBot(): void {
  const spawnNew = () => {
    console.log("Starting Telegram Bot child process...");
    botStatus = "Starting";

    // Use absolute path or execute in current folder
    botProcess = spawn("python3", ["bot.py"], { cwd: process.cwd() });
    botStatus = "Running";

    // BUG FIX: spawn() emits "error" separately from "close" (e.g. if python3 isn't found).
    // Without a listener, that "error" event is unhandled and crashes the whole Node
    // process - taking down the dashboard along with the bot it was trying to start.
    botProcess.on("error", (err) => {
      console.error("[Bot Err] Failed to start bot.py:", err);
      botLogs.push(`[${new Date().toLocaleTimeString()} ERR] Failed to start: ${err.message}`);
      botStatus = "Stopped";
      botProcess = null;
    });

    botProcess.stdout?.on("data", (data) => {
      const text = data.toString().trim();
      if (text) {
        console.log(`[Bot Out]: ${text}`);
        botLogs.push(`[${new Date().toLocaleTimeString()}] ${text}`);
        if (botLogs.length > 200) botLogs.shift();
      }
    });

    botProcess.stderr?.on("data", (data) => {
      const text = data.toString().trim();
      if (text) {
        console.error(`[Bot Err]: ${text}`);
        botLogs.push(`[${new Date().toLocaleTimeString()} ERR] ${text}`);
        if (botLogs.length > 200) botLogs.shift();
      }
    });

    botProcess.on("close", (code) => {
      console.log(`Telegram Bot stopped with code ${code}`);
      botStatus = "Stopped";
      botProcess = null;
    });
  };

  if (botProcess) {
    // BUG FIX (race condition): kill() only sends SIGTERM - it doesn't wait for the
    // process to actually exit. Spawning the replacement immediately after calling kill()
    // could leave two bot.py instances polling Telegram's getUpdates at the same time,
    // which Telegram answers with "Conflict: terminated by other getUpdates request" and
    // can let both instances briefly handle the same incoming message. Wait for the old
    // process to fully exit before starting the new one.
    const old = botProcess;
    old.once("close", spawnNew);
    old.kill();
  } else {
    spawnNew();
  }
}

// Automatically start the bot
startBot();

// API: Bot Status and Controls
app.get("/api/bot/status", (req, res) => {
  res.json({
    status: botStatus,
    logs: botLogs,
    hasToken: !!process.env.TELEGRAM_TOKEN,
    hasGroqKey: !!process.env.GROQ_API_KEY
  });
});

app.post("/api/bot/control", (req, res) => {
  const { action } = req.body;
  if (action === "restart") {
    startBot();
    res.json({ status: "ok", message: "Bot restarting..." });
  } else if (action === "stop") {
    if (botProcess) {
      botProcess.kill();
      res.json({ status: "ok", message: "Bot stopped." });
    } else {
      res.json({ status: "error", message: "Bot was not running." });
    }
  } else {
    res.status(400).json({ error: "Invalid action" });
  }
});

// API: Workers from workers.db
app.get("/api/workers", async (req, res) => {
  const pyCode = `
import sqlite3, json, os
if not os.path.exists("workers.db"):
    print("[]")
else:
    conn = sqlite3.connect("workers.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM workers ORDER BY position, sort_order, last_name, first_name").fetchall()
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        conn.close()
  `;
  try {
    const workers = await runPythonQuery(pyCode);
    res.json(workers);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// API: Reports from workers.db
app.get("/api/reports", async (req, res) => {
  const pyCode = `
import sqlite3, json, os
if not os.path.exists("workers.db"):
    print("[]")
else:
    conn = sqlite3.connect("workers.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT r.*, w.last_name, w.first_name, w.position 
            FROM reports r 
            LEFT JOIN workers w ON r.telegram_id = w.telegram_id 
            ORDER BY r.report_date DESC, r.received_at DESC 
            LIMIT 100
        """).fetchall()
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        conn.close()
  `;
  try {
    const reports = await runPythonQuery(pyCode);
    res.json(reports);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// API: Objects / Groups from workers.db
app.get("/api/objects", async (req, res) => {
  const pyCode = `
import sqlite3, json, os
if not os.path.exists("workers.db"):
    print("[]")
else:
    conn = sqlite3.connect("workers.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM objects").fetchall()
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        conn.close()
  `;
  try {
    const objects = await runPythonQuery(pyCode);
    res.json(objects);
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

// Vite & Static Asset Setup
async function startServer() {
  if (process.env.NODE_ENV !== "production") {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(process.cwd(), "dist");
    app.use(express.static(distPath));
    app.get("*", (req, res) => {
      res.sendFile(path.join(distPath, "index.html"));
    });
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`Express and Vite server running on http://localhost:${PORT}`);
  });
}

// BUG FIX: startServer() is async and its rejection was never handled - if createViteServer()
// or app.listen() ever throws, this becomes an unhandled promise rejection instead of a
// visible startup error.
startServer().catch((err) => {
  console.error("Fatal error starting server:", err);
  process.exit(1);
});

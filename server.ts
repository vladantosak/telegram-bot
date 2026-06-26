import express from "express";
import path from "path";
import { createServer as createViteServer } from "vite";
import sqlite3 from "sqlite3";
import { open } from "sqlite";

async function startServer() {
  const app = express();
  const PORT = 3000;

  app.use(express.json());

  // Open SQLite database connection
  const db = await open({
    filename: 'workers.db',
    driver: sqlite3.Database
  });

  // Ensure tables and object_id column exist in db in case server starts before bot
  try {
    await db.exec(`
      CREATE TABLE IF NOT EXISTS workers (
        telegram_id INTEGER PRIMARY KEY,
        last_name TEXT NOT NULL,
        first_name TEXT NOT NULL,
        position TEXT NOT NULL DEFAULT 'Не указано',
        group_id INTEGER NOT NULL,
        schedule TEXT NOT NULL DEFAULT 'A',
        needs_daily_fact INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        object_id TEXT NOT NULL DEFAULT 'Основной'
      )
    `);
    
    // Add columns if they don't exist
    const cols = (await db.all("PRAGMA table_info(workers)")).map((r: any) => r.name);
    if (!cols.includes("object_id")) {
      await db.exec("ALTER TABLE workers ADD COLUMN object_id TEXT NOT NULL DEFAULT 'Основной'");
    }
  } catch (err) {
    console.error("Database schema setup error on express server side:", err);
  }

  // API Endpoints
  app.get("/api/workers", async (req, res) => {
    try {
      const workers = await db.all("SELECT * FROM workers ORDER BY last_name, first_name");
      res.json(workers);
    } catch (e: any) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get("/api/departments", async (req, res) => {
    try {
      const depts = await db.all("SELECT DISTINCT position FROM workers WHERE position IS NOT NULL AND position != '' ORDER BY position");
      res.json(depts.map(d => d.position));
    } catch (e: any) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get("/api/objects", async (req, res) => {
    try {
      const objects = await db.all("SELECT DISTINCT object_id FROM workers WHERE object_id IS NOT NULL AND object_id != '' ORDER BY object_id");
      res.json(objects.map(o => o.object_id));
    } catch (e: any) {
      res.status(500).json({ error: e.message });
    }
  });

  app.get("/api/reports", async (req, res) => {
    try {
      const { startDate, endDate, workerId, department, objectId, isOk, search } = req.query;
      
      let query = `
        SELECT r.*, w.last_name, w.first_name, w.position as department, w.object_id
        FROM reports r
        LEFT JOIN workers w ON r.telegram_id = w.telegram_id
        WHERE 1=1
      `;
      const params: any[] = [];

      if (startDate) {
        query += " AND r.report_date >= ?";
        params.push(startDate);
      }
      if (endDate) {
        query += " AND r.report_date <= ?";
        params.push(endDate);
      }
      if (workerId) {
        query += " AND r.telegram_id = ?";
        params.push(Number(workerId));
      }
      if (department) {
        query += " AND lower(w.position) = lower(?)";
        params.push(department);
      }
      if (objectId) {
        query += " AND lower(w.object_id) = lower(?)";
        params.push(objectId);
      }
      if (isOk !== undefined && isOk !== "") {
        query += " AND r.is_ok = ?";
        params.push(isOk === "true" ? 1 : 0);
      }
      if (search) {
        query += " AND (r.raw_text LIKE ? OR r.format_comment LIKE ? OR r.required_action LIKE ? OR w.last_name LIKE ? OR w.first_name LIKE ?)";
        const searchLike = `%${search}%`;
        params.push(searchLike, searchLike, searchLike, searchLike, searchLike);
      }

      query += " ORDER BY r.report_date DESC, r.received_at DESC";
      
      const reports = await db.all(query, params);
      res.json(reports);
    } catch (e: any) {
      res.status(500).json({ error: e.message });
    }
  });

  // Vite middleware for development
  if (process.env.NODE_ENV !== "production") {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa",
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('*', (req, res) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`Server running on http://localhost:${PORT}`);
  });
}

startServer();

import React, { useState, useEffect } from "react";
import { 
  Users, 
  FileText, 
  Settings, 
  Terminal, 
  RefreshCw, 
  Play, 
  Square, 
  AlertCircle, 
  CheckCircle2, 
  XCircle, 
  MapPin, 
  Search,
  MessageCircle
} from "lucide-react";

interface Worker {
  telegram_id: number;
  last_name: string;
  first_name: string;
  position: string;
  group_id: number;
  schedule: string;
  needs_daily_fact: number;
  sort_order: number;
  is_active: number;
  object_id: string;
}

interface Report {
  id: number;
  telegram_id: number;
  report_date: string;
  report_type: string;
  slot_time: string | null;
  received_at: string;
  is_ok: number;
  is_late: number;
  format_comment: string | null;
  required_action: string | null;
  raw_text: string;
  last_name?: string;
  first_name?: string;
  position?: string;
}

interface BotStatus {
  status: string;
  logs: string[];
  hasToken: boolean;
  hasGroqKey: boolean;
}

export default function App() {
  const [activeTab, setActiveTab] = useState<"bot" | "workers" | "reports">("bot");
  
  // Bot control states
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [isRestarting, setIsRestarting] = useState(false);

  // Data states
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState("");

  const fetchBotStatus = async () => {
    try {
      const res = await fetch("/api/bot/status");
      const data = await res.json();
      setBotStatus(data);
      if (data.logs) {
        setLogs(data.logs);
      }
    } catch (e) {
      console.error("Error fetching bot status", e);
    }
  };

  const fetchData = async () => {
    setLoading(true);
    try {
      const [workersRes, reportsRes] = await Promise.all([
        fetch("/api/workers"),
        fetch("/api/reports")
      ]);
      const workersData = await workersRes.json();
      const reportsData = await reportsRes.json();
      if (Array.isArray(workersData)) setWorkers(workersData);
      if (Array.isArray(reportsData)) setReports(reportsData);
    } catch (e) {
      console.error("Error fetching data", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchBotStatus();
    fetchData();
    const interval = setInterval(fetchBotStatus, 3000);
    return () => clearInterval(interval);
  }, []);

  const handleBotAction = async (action: "restart" | "stop") => {
    if (action === "restart") setIsRestarting(true);
    try {
      await fetch("/api/bot/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action })
      });
      setTimeout(() => {
        fetchBotStatus();
        setIsRestarting(false);
      }, 1500);
    } catch (e) {
      console.error("Error sending bot control command", e);
      setIsRestarting(false);
    }
  };

  const filteredWorkers = workers.filter(w => {
    const query = searchQuery.toLowerCase();
    return (
      `${w.last_name} ${w.first_name}`.toLowerCase().includes(query) ||
      w.position.toLowerCase().includes(query) ||
      (w.object_id && w.object_id.toLowerCase().includes(query))
    );
  });

  return (
    <div id="dashboard-root" className="min-h-screen bg-slate-50 text-slate-900 font-sans">
      {/* Top Header Navigation */}
      <header id="main-header" className="bg-white border-b border-slate-200 sticky top-0 z-10 shadow-sm">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="bg-indigo-600 p-2 rounded-lg text-white">
              <CheckCircle2 className="w-6 h-6" />
            </div>
            <div>
              <h1 className="text-xl font-bold tracking-tight text-slate-900">Прораб-Бот Панель</h1>
              <p className="text-xs text-gray-500">Система контроля видеоотчетов и фактов с ИИ</p>
            </div>
          </div>
          
          <div className="flex space-x-1 bg-slate-100 p-1 rounded-xl">
            <button
              id="tab-bot"
              onClick={() => setActiveTab("bot")}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all flex items-center space-x-2 ${
                activeTab === "bot" 
                  ? "bg-white text-slate-900 shadow-xs" 
                  : "text-slate-600 hover:text-slate-900"
              }`}
            >
              <Terminal className="w-4 h-4 mr-1.5" />
              Статус бора
            </button>
            <button
              id="tab-workers"
              onClick={() => {
                fetchData();
                setActiveTab("workers");
              }}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all flex items-center gap-2 ${
                activeTab === "workers" ? "bg-white text-slate-900 shadow-xs" : "text-slate-600 hover:text-slate-900"
              }`}
            >
              <Users className="w-4 h-4 mr-1.5" />
              Сотрудники
            </button>
            <button
              id="tab-reports"
              onClick={() => {
                fetchData();
                setActiveTab("reports");
              }}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all flex items-center space-x-2 ${
                activeTab === "reports" 
                  ? "bg-white text-slate-900 shadow-xs" 
                  : "text-slate-600 hover:text-slate-900"
              }`}
            >
              <FileText className="w-4 h-4 mr-1.5" />
              История отчетов
            </button>
          </div>
        </div>
      </header>

      {/* Main Body Grid */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {activeTab === "bot" && (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
            {/* Bot Controls & API State */}
            <div className="space-y-6">
              <div className="bg-white rounded-2xl p-6 border border-slate-200 shadow-xs">
                <h2 className="text-lg font-bold mb-4 flex items-center gap-2">
                  <Settings className="w-5 h-5 text-indigo-600" />
                  Управление сервером
                </h2>
                
                <div className="mb-6 p-4 rounded-xl bg-slate-50 border border-slate-100 flex items-center justify-between">
                  <div>
                    <div className="text-xs text-slate-500 uppercase tracking-wider font-semibold">Статус службы</div>
                    <div className="text-lg font-bold flex items-center gap-2 mt-1">
                      <span className={`w-3 h-3 rounded-full inline-block ${
                        botStatus?.status === "Running" ? "bg-emerald-500 animate-pulse" : "bg-rose-500"
                      }`} />
                      {botStatus?.status || "Loading..."}
                    </div>
                  </div>
                  
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleBotAction("restart")}
                      disabled={isRestarting}
                      className="p-2.5 bg-indigo-50 text-indigo-600 rounded-xl hover:bg-indigo-100 transition-colors disabled:opacity-50"
                      title="Перезагрузить"
                    >
                      <RefreshCw className={`w-5 h-5 ${isRestarting ? "animate-spin" : ""}`} />
                    </button>
                    {botStatus?.status === "Running" ? (
                      <button
                        onClick={() => handleBotAction("stop")}
                        className="p-2.5 bg-rose-50 text-rose-600 rounded-xl hover:bg-rose-100 transition-colors"
                        title="Остановить"
                      >
                        <Square className="w-5 h-5" />
                      </button>
                    ) : (
                      <button
                        onClick={() => handleBotAction("restart")}
                        className="p-2.5 bg-emerald-50 text-emerald-600 rounded-xl hover:bg-emerald-100 transition-colors"
                        title="Запустить"
                      >
                        <Play className="w-5 h-5" />
                      </button>
                    )}
                  </div>
                </div>

                <div className="space-y-4">
                  <div className="flex items-center justify-between py-2 border-b border-slate-100">
                    <span className="text-sm text-slate-600">Telegram Token</span>
                    {botStatus?.hasToken ? (
                      <span className="text-xs px-2.5 py-1 bg-emerald-50 text-emerald-700 font-semibold rounded-full flex items-center gap-1">
                        <CheckCircle2 className="w-3.5 h-3.5" /> Настроен
                      </span>
                    ) : (
                      <span className="text-xs px-2.5 py-1 bg-rose-50 text-rose-700 font-semibold rounded-full flex items-center gap-1">
                        <AlertCircle className="w-3.5 h-3.5" /> Отсутствует
                      </span>
                    )}
                  </div>

                  <div className="flex items-center justify-between py-2 border-b border-slate-100">
                    <span className="text-sm text-slate-600">Groq API Key (Llama/Whisper)</span>
                    {botStatus?.hasGroqKey ? (
                      <span className="text-xs px-2.5 py-1 bg-emerald-50 text-emerald-700 font-semibold rounded-full flex items-center gap-1">
                        <CheckCircle2 className="w-3.5 h-3.5" /> Настроен
                      </span>
                    ) : (
                      <span className="text-xs px-2.5 py-1 bg-rose-50 text-rose-700 font-semibold rounded-full flex items-center gap-1">
                        <AlertCircle className="w-3.5 h-3.5" /> Отсутствует
                      </span>
                    )}
                  </div>
                </div>
              </div>

              {/* Instructions Panel */}
              <div className="bg-white rounded-2xl p-6 border border-slate-200 shadow-xs">
                <h2 className="text-lg font-bold mb-3">Как работает бот?</h2>
                <ul className="space-y-2.5 text-sm text-slate-600">
                  <li className="flex items-start gap-2">
                    <span className="w-5 h-5 rounded-full bg-slate-100 flex items-center justify-center text-xs font-bold mt-0.5">1</span>
                    Сотрудники отправляют видео-статусы в течение дня по своему графику.
                  </li>
                  <li className="flex items-start gap-2">
                    <span className="w-5 h-5 rounded-full bg-slate-100 flex items-center justify-center text-xs font-bold mt-0.5">2</span>
                    Бот собирает видео в пачки за 8 секунд и объединяет их в один отчет.
                  </li>
                  <li className="flex items-start gap-2">
                    <span className="w-5 h-5 rounded-full bg-slate-100 flex items-center justify-center text-xs font-bold mt-0.5">3</span>
                    ИИ оценивает каждое видео индивидуально, выставляет общую оценку (ОК если хоть одно видео ОК).
                  </li>
                  <li className="flex items-start gap-2">
                    <span className="w-5 h-5 rounded-full bg-slate-100 flex items-center justify-center text-xs font-bold mt-0.5">4</span>
                    Оценка статуса отправляется **только в чат группы** и никогда не спамит админу!
                  </li>
                </ul>
              </div>
            </div>

            {/* Stream Console logs */}
            <div className="lg:col-span-2 bg-slate-900 rounded-2xl p-6 border border-slate-800 flex flex-col h-[500px]">
              <div className="flex items-center justify-between pb-4 border-b border-slate-800 mb-4">
                <div className="flex items-center gap-2 text-slate-200 font-bold">
                  <Terminal className="w-5 h-5 text-indigo-400" />
                  Терминал логов бота
                </div>
                <span className="text-xs px-2 py-1 bg-slate-800 text-slate-400 font-mono rounded">bot.log</span>
              </div>

              <div className="flex-1 overflow-y-auto font-mono text-xs text-slate-300 space-y-1.5 scrollbar-thin scrollbar-thumb-slate-800">
                {logs.length === 0 ? (
                  <div className="text-slate-500 italic py-8 text-center">Ожидание логов запуска службы...</div>
                ) : (
                  logs.map((log, idx) => (
                    <div key={idx} className="whitespace-pre-wrap leading-relaxed py-0.5 border-b border-slate-850 hover:bg-slate-850 px-1 rounded">
                      {log}
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}

        {activeTab === "workers" && (
          <div className="bg-white rounded-2xl border border-slate-200 shadow-xs overflow-hidden">
            {/* Search filter bar */}
            <div className="p-6 border-b border-slate-200 flex flex-col md:flex-row md:items-center justify-between gap-4 bg-slate-50/50">
              <div>
                <h2 className="text-lg font-bold text-slate-900">Список сотрудников</h2>
                <p className="text-sm text-gray-500">Всего зарегистрировано сотрудников в базе: {workers.length}</p>
              </div>

              <div className="relative max-w-md w-full">
                <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                <input
                  type="text"
                  placeholder="Поиск по ФИО, должности или объекту..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="w-full pl-10 pr-4 py-2 bg-white border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500"
                />
              </div>
            </div>

            {loading ? (
              <div className="py-24 text-center text-slate-500 italic">Загрузка данных сотрудников...</div>
            ) : filteredWorkers.length === 0 ? (
              <div className="py-24 text-center text-slate-500 italic">Сотрудники не найдены.</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse">
                  <thead>
                    <tr className="bg-slate-50 text-slate-500 text-xs font-semibold uppercase tracking-wider border-b border-slate-200">
                      <th className="py-4 px-6">Сотрудник</th>
                      <th className="py-4 px-6">Объект</th>
                      <th className="py-4 px-6">Должность</th>
                      <th className="py-4 px-6">ID Группы</th>
                      <th className="py-4 px-6">График</th>
                      <th className="py-4 px-6">Факт</th>
                      <th className="py-4 px-6">Статус</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-150 text-sm">
                    {filteredWorkers.map((w) => (
                      <tr key={w.telegram_id} className="hover:bg-slate-50/50">
                        <td className="py-4 px-6 font-medium text-slate-950">
                          {w.last_name} {w.first_name}
                          <div className="text-xs text-slate-400 font-mono mt-0.5">ID: {w.telegram_id}</div>
                        </td>
                        <td className="py-4 px-6 text-slate-600">
                          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 bg-slate-100 text-slate-700 rounded-lg text-xs font-semibold">
                            <MapPin className="w-3.5 h-3.5 text-indigo-500" />
                            {w.object_id || "Основной"}
                          </span>
                        </td>
                        <td className="py-4 px-6 text-slate-600">{w.position}</td>
                        <td className="py-4 px-6 text-slate-600 font-mono text-xs">{w.group_id}</td>
                        <td className="py-4 px-6">
                          <span className="px-2.5 py-1 bg-indigo-50 text-indigo-700 font-semibold rounded-lg text-xs">
                            Схема {w.schedule}
                          </span>
                        </td>
                        <td className="py-4 px-6">
                          {w.needs_daily_fact ? (
                            <span className="text-emerald-600 font-medium">Да</span>
                          ) : (
                            <span className="text-slate-400">Нет</span>
                          )}
                        </td>
                        <td className="py-4 px-6">
                          {w.is_active ? (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-800">
                              Работает
                            </span>
                          ) : (
                            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-amber-50 text-amber-800">
                              Отпуск / БЛ
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {activeTab === "reports" && (
          <div className="bg-white rounded-2xl border border-slate-200 shadow-xs overflow-hidden">
            <div className="p-6 border-b border-slate-200 bg-slate-50/50">
              <h2 className="text-lg font-bold text-slate-900">Последние отчеты в системе</h2>
              <p className="text-sm text-gray-500">Последние 100 отчетов сотрудников, сохраненных в БД</p>
            </div>

            {loading ? (
              <div className="py-24 text-center text-slate-500 italic">Загрузка истории отчетов...</div>
            ) : reports.length === 0 ? (
              <div className="py-24 text-center text-slate-500 italic">История отчетов пока пуста.</div>
            ) : (
              <div className="divide-y divide-slate-150">
                {reports.map((r) => (
                  <div key={r.id} className="p-6 hover:bg-slate-50/30 transition-all flex flex-col md:flex-row md:items-start justify-between gap-6">
                    <div className="space-y-3 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-bold text-slate-900">
                          {r.last_name || "Неизвестный"} {r.first_name || "Сотрудник"}
                        </span>
                        <span className="text-xs text-slate-400">({r.position || "нет должности"})</span>
                        
                        <span className="text-xs px-2.5 py-0.5 bg-slate-100 text-slate-600 rounded-md font-medium">
                          {r.report_date}
                        </span>
                        <span className="text-xs text-slate-400 font-mono">
                          {r.received_at}
                        </span>
                      </div>

                      <div className="text-xs font-semibold px-2 py-1 bg-indigo-50/60 text-indigo-700 rounded inline-block">
                        {r.report_type === "status" ? `⏱ Статус за слот ${r.slot_time || "Не указан"}` : "📋 Итог дня / Факт"}
                        {r.is_late === 1 && <span className="text-rose-600 ml-1.5 font-bold">⏰ Опоздание</span>}
                      </div>

                      <div className="p-3 bg-slate-50 rounded-xl border border-slate-100">
                        <div className="text-xs text-slate-400 font-semibold mb-1 uppercase tracking-wider">Оригинальный текст / транскрипт:</div>
                        <p className="text-sm text-slate-700 whitespace-pre-wrap leading-relaxed">
                          {r.raw_text}
                        </p>
                      </div>
                    </div>

                    <div className="flex flex-col items-end gap-2 w-full md:w-64 min-w-[240px]">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs text-slate-500 font-medium">Оценка ИИ:</span>
                        {r.is_ok ? (
                          <span className="px-2.5 py-1 bg-emerald-50 text-emerald-800 text-xs font-bold rounded-full flex items-center gap-1">
                            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" /> ОК
                          </span>
                        ) : (
                          <span className="px-2.5 py-1 bg-rose-50 text-rose-800 text-xs font-bold rounded-full flex items-center gap-1">
                            <XCircle className="w-3.5 h-3.5 text-rose-500" /> НЕ ОК
                          </span>
                        )}
                      </div>

                      <div className="text-xs text-right w-full mt-2">
                        <div className="text-slate-400 font-semibold mb-1 uppercase tracking-wider">Комментарий ИИ / Оценка:</div>
                        <p className="text-slate-700 italic leading-relaxed">
                          {r.format_comment || "Нет комментария"}
                        </p>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}

// Simple legacy wrapper support if icons have differences in name string
const LucideIconWrapper = ({ name, ...props }: { name: string; [key: string]: any }) => {
  return <span {...props}>[{name}]</span>;
};
